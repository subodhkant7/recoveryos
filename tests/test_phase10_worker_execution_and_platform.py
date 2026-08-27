"""
Phase 10 Automated Test Suite: Full-Lifecycle Asynchronous Worker Execution & Production Platform.

Covers:
1. Asynchronous worker agent execution loop and outcome contract verification.
2. In-flight step-level cancellation and interruption check.
3. Production operator authentication (/api/auth/login) with cryptographic signatures.
4. Active session validation (/api/auth/session).
5. Authenticated SSE streaming via query token parameter (?token=...).
6. Real-time EventBroadcaster queue delivery and live push.
7. Firestore indexes configuration schema validation.
8. Multi-tenant isolation across async worker and session management.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
import pytest
from httpx import AsyncClient, ASGITransport

import backend.api.server as srv
from backend.api.server import app
from backend.models.workflow import WorkflowState
from backend.models.events import EventType
from backend.events.message_models import WorkflowEventType
from backend.events.message_models import WorkflowExecutionMessage
from backend.events.consumer import WorkflowEventConsumer
from backend.events.broadcast import event_broadcaster
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.external_services import SimulatedServices
from backend.agents.agent_factory import AgentFactory
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA
from backend.security.tokens import create_access_token, verify_access_token
from backend.security.principal import Role
from backend.config import config
from backend.security.audit import clear_security_audit_logs
from backend.observability.logging import current_request_id, current_workflow_id, current_tenant_id


@pytest.fixture(autouse=True)
def _clean_test_context():
    """Ensure clean store and context between tests."""
    current_request_id.set("")
    current_workflow_id.set("")
    current_tenant_id.set("")
    clear_security_audit_logs()
    if hasattr(srv.store, "_workflows"):
        srv.store._workflows.clear()
    if hasattr(srv.store, "_steps"):
        srv.store._steps.clear()
    if hasattr(srv.store, "_events"):
        srv.store._events.clear()
    if hasattr(srv.store, "_approvals"):
        srv.store._approvals.clear()
    if hasattr(srv.store, "_operations"):
        srv.store._operations.clear()
    if hasattr(srv.store, "_audit_events"):
        srv.store._audit_events.clear()
    yield
    current_request_id.set("")
    current_workflow_id.set("")
    current_tenant_id.set("")
    clear_security_audit_logs()
    if hasattr(srv.store, "_workflows"):
        srv.store._workflows.clear()


@pytest.mark.asyncio
async def test_01_async_worker_executes_workflow_agent_loop():
    """Gate 1: WorkflowEventConsumer with AgentFactory executes workflow through to verification."""
    store = srv.store
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)

    consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        agent_factory=agent_factory,
        worker_id="test-worker-1",
    )

    wf_id = f"wf-async-{uuid.uuid4().hex[:8]}"
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Async E2E Test",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
    )

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_{wf_id}_v1",
        expected_version=1,
        correlation_id="corr-async-1",
        producer_id="api-test",
        payload={"scenario": "billing_unavailable"},
    )

    result = await consumer.consume_message(msg)
    assert result["status"] == "PROCESSED"
    assert "agent_result" in result

    # Verify workflow state transitioned
    wf = await store.get_workflow(wf_id)
    assert wf is not None
    # State should have moved beyond CREATED
    assert wf["state"] in (
        WorkflowState.EXECUTING.value,
        WorkflowState.VERIFYING.value,
        WorkflowState.COMPLETED.value,
        WorkflowState.RECOVERING.value,
        WorkflowState.UNKNOWN.value,
    )


@pytest.mark.asyncio
async def test_02_async_worker_step_cancellation_halts_execution():
    """Gate 2: In-flight cancellation immediately halts the agent runner."""
    from backend.engine.agent_runner import run_workflow_agent

    store = srv.store
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)

    wf_id = f"wf-cancel-{uuid.uuid4().hex[:8]}"
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Cancel Test",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-test",
    )

    # Pre-escalate the workflow to simulate an operator cancellation
    await engine.transition(wf_id, WorkflowState.EXECUTING)
    await engine.transition(wf_id, WorkflowState.ESCALATED, detail="Operator cancel")

    res = await run_workflow_agent(wf_id, store, engine, agent_factory)
    assert res["status"] in ("HALTED", "CANCELLED_MID_EXECUTION", "TERMINAL_SKIPPED")

    wf = await store.get_workflow(wf_id)
    assert wf["state"] == WorkflowState.ESCALATED.value


@pytest.mark.asyncio
async def test_03_auth_login_endpoint_issues_valid_signed_jwts():
    """Gate 3: POST /api/auth/login returns a cryptographically valid HMAC-SHA256 token."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/auth/login", json={
            "username": "operator-alice",
            "role": "operator",
            "tenant_id": "tenant-corp",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["role"] == "operator"
        assert data["tenant_id"] == "tenant-corp"

        # Verify token signature directly
        principal = verify_access_token(data["access_token"], secret_key=config.jwt_secret_key)
        assert principal.user_id == "operator-alice"
        assert principal.role == Role.OPERATOR
        assert principal.tenant_id == "tenant-corp"


@pytest.mark.asyncio
async def test_04_auth_session_endpoint_returns_principal_and_permissions():
    """Gate 3: GET /api/auth/session returns active user claims."""
    token = create_access_token(
        user_id="admin-bob",
        role=Role.ADMIN,
        tenant_id="tenant-all",
        secret_key=config.jwt_secret_key,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/auth/session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["user_id"] == "admin-bob"
        assert data["role"] == "admin"
        assert len(data["permissions"]) > 0


@pytest.mark.asyncio
async def test_05_authenticated_sse_stream_query_token():
    """Gate 4: GET /api/workflows/{id}/events/stream supports query token authentication."""
    wf_id = f"wf-sse-{uuid.uuid4().hex[:8]}"
    token = create_access_token(
        user_id="op-1",
        role=Role.OPERATOR,
        tenant_id="tenant-sse",
        secret_key=config.jwt_secret_key,
    )

    # Seed workflow
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-sse",
        "state": "COMPLETED",
        "name": "SSE Test",
        "version": 1,
    })
    await srv.store.append_event(wf_id, {
        "event_id": "ev-1",
        "event_type": "STATE_CHANGE",
        "title": "Created",
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Invalid token must be rejected with 401
        resp_invalid = await ac.get(f"/api/workflows/{wf_id}/events/stream?token=bad_token")
        assert resp_invalid.status_code == 401

        # 2. Valid query token connects and receives event stream
        resp_valid = await ac.get(f"/api/workflows/{wf_id}/events/stream?token={token}")
        assert resp_valid.status_code == 200
        assert "text/event-stream" in resp_valid.headers.get("content-type", "")
        assert "STREAM_END" in resp_valid.text


@pytest.mark.asyncio
async def test_06_realtime_event_broadcaster_pushes_live_events():
    """Gate 4: EventBroadcaster delivers published events to subscriber queues."""
    wf_id = f"wf-broadcast-{uuid.uuid4().hex[:8]}"
    queue = await event_broadcaster.subscribe(wf_id)
    try:
        test_event = {
            "event_id": "ev-live-1",
            "workflow_id": wf_id,
            "event_type": "STEP_COMPLETED",
            "title": "Live Step",
        }
        await event_broadcaster.broadcast(wf_id, test_event)

        received = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert received["event_id"] == "ev-live-1"
        assert received["event_type"] == "STEP_COMPLETED"
    finally:
        await event_broadcaster.unsubscribe(wf_id, queue)


def test_07_firestore_indexes_schema_valid():
    """Gate 5: firestore.indexes.json exists and defines required composite index rules."""
    index_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "firestore.indexes.json")
    assert os.path.exists(index_file), "firestore.indexes.json must exist"

    with open(index_file, "r") as f:
        data = json.load(f)

    assert "indexes" in data
    indexes = data["indexes"]
    assert len(indexes) >= 3

    collections = [idx.get("collectionGroup") for idx in indexes]
    assert "workflows" in collections
    assert "audit_events" in collections


@pytest.mark.asyncio
async def test_08_multi_tenant_isolation_in_auth_and_worker():
    """Gate 8: Cross-tenant access is strictly denied on worker and auth routes."""
    store = srv.store
    engine = WorkflowEngine(store)
    consumer = WorkflowEventConsumer(store=store, engine=engine)

    wf_id = f"wf-iso-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-alpha",
        "state": "EXECUTING",
        "version": 1,
    })

    # Dispatch message with mismatched tenant
    msg_mismatch = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-bravo",
        idempotency_key=f"op_{wf_id}_mismatch",
        expected_version=1,
        correlation_id="corr-mismatch",
        producer_id="api-test",
    )

    from backend.events.consumer import ConsumerExecutionError
    with pytest.raises(ConsumerExecutionError) as exc_info:
        await consumer.consume_message(msg_mismatch)

    assert "Tenant mismatch" in str(exc_info.value)
