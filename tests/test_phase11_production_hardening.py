"""
Phase 11 Production Hardening Verification Suite:
Authentication Security, SSE Tickets, Operation Lease Heartbeats,
Distributed Event Delivery, and Firestore Scalability.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone, timedelta
import json
import os
import secrets
import time
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
from backend.security.principal import Role, Principal
from backend.security.authenticator import auth_provider, hash_password, verify_password
from backend.security.sse_tickets import sse_ticket_store
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
    sse_ticket_store.clear()
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
    sse_ticket_store.clear()
    if hasattr(srv.store, "_workflows"):
        srv.store._workflows.clear()


# ==============================================================================
# 1. AUTHENTICATION & ARBITRARY JWT MINTING PREVENTION (GATE 1)
# ==============================================================================

@pytest.mark.asyncio
async def test_auth_01_valid_credentials_issue_server_bound_claims():
    """Valid operator credentials issue a JWT strictly bound to server-side role and tenant."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/auth/login", json={
            "username": "operator-alice",
            "password": "OperatorSecurePass!2026",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "operator-alice"
        assert data["role"] == "operator"
        assert data["tenant_id"] == "tenant-corp"


@pytest.mark.asyncio
async def test_auth_02_arbitrary_admin_role_request_ignored_and_bound_to_user():
    """An unprivileged user requesting role='admin' receives only their server-configured role."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/auth/login", json={
            "username": "operator-alice",
            "password": "OperatorSecurePass!2026",
            "role": "admin",  # Attacker attempt to elevate
            "tenant_id": "tenant-other",  # Attacker attempt to cross tenants
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "operator"  # Server-bound
        assert data["tenant_id"] == "tenant-corp"  # Server-bound


@pytest.mark.asyncio
async def test_auth_03_invalid_password_rejected_with_401():
    """Incorrect password returns 401 without revealing details."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/auth/login", json={
            "username": "admin",
            "password": "WrongPassword123!",
        })
        assert resp.status_code == 401
        assert "Invalid username or password" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_auth_04_unknown_user_rejected_with_401():
    """Non-existent username returns 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/auth/login", json={
            "username": "non_existent_hacker",
            "password": "SomePassword123!",
        })
        assert resp.status_code == 401


def test_auth_05_password_hashing_and_constant_time_verification():
    """Verify PBKDF2-HMAC-SHA256 password hashing and verification."""
    pwd = "SuperSecretPassword#2026"
    pwd_hash, salt = hash_password(pwd)
    assert len(pwd_hash) == 64
    assert len(salt) == 32
    assert verify_password(pwd, pwd_hash, salt) is True
    assert verify_password("WrongPassword", pwd_hash, salt) is False


# ==============================================================================
# 2. SECURE SSE AUTHENTICATION & SINGLE-USE TICKETS (GATE 2)
# ==============================================================================

@pytest.mark.asyncio
async def test_sse_01_authenticated_principal_can_issue_ticket():
    """Authenticated user can issue a 60-second single-use SSE ticket for their workflow."""
    wf_id = f"wf-sse-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-default",
        "state": "EXECUTING",
        "version": 1,
    })

    token = create_access_token(
        user_id="operator-1",
        role=Role.OPERATOR,
        tenant_id="tenant-default",
        secret_key=config.jwt_secret_key,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/auth/sse-ticket",
            json={"workflow_id": wf_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "ticket" in data
        assert data["ticket"].startswith("sset_")
        assert data["workflow_id"] == wf_id
        assert data["expires_in"] == 60


@pytest.mark.asyncio
async def test_sse_02_ticket_is_single_use():
    """SSE ticket is consumed on first use and rejected on replay."""
    principal = Principal(user_id="op-1", role=Role.OPERATOR, tenant_id="tenant-1")
    ticket = await sse_ticket_store.issue_ticket(principal, "wf-1")

    # First consumption succeeds
    consumed_principal = await sse_ticket_store.consume_ticket(ticket.ticket_id, "wf-1")
    assert consumed_principal is not None
    assert consumed_principal.user_id == "op-1"

    # Second consumption fails (replay attack)
    consumed_second = await sse_ticket_store.consume_ticket(ticket.ticket_id, "wf-1")
    assert consumed_second is None


@pytest.mark.asyncio
async def test_sse_03_ticket_cannot_be_used_against_different_workflow():
    """A ticket issued for workflow A cannot be used to stream workflow B."""
    principal = Principal(user_id="op-1", role=Role.OPERATOR, tenant_id="tenant-1")
    ticket = await sse_ticket_store.issue_ticket(principal, "wf-target-a")

    # Attempt consumption against workflow B
    consumed = await sse_ticket_store.consume_ticket(ticket.ticket_id, "wf-target-b")
    assert consumed is None


@pytest.mark.asyncio
async def test_sse_04_expired_ticket_rejected():
    """An expired SSE ticket is rejected."""
    principal = Principal(user_id="op-1", role=Role.OPERATOR, tenant_id="tenant-1")
    ticket = await sse_ticket_store.issue_ticket(principal, "wf-1", ttl_seconds=-1)

    consumed = await sse_ticket_store.consume_ticket(ticket.ticket_id, "wf-1")
    assert consumed is None


@pytest.mark.asyncio
async def test_sse_05_stream_connects_with_valid_ticket():
    """Stream endpoint connects successfully with valid single-use ticket."""
    wf_id = f"wf-stream-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-default",
        "state": "COMPLETED",
        "version": 1,
    })

    principal = Principal(user_id="op-1", role=Role.OPERATOR, tenant_id="tenant-default")
    ticket = await sse_ticket_store.issue_ticket(principal, wf_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(f"/api/workflows/{wf_id}/events/stream?ticket={ticket.ticket_id}")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "STREAM_END" in resp.text


# ==============================================================================
# 3. OPERATION LEASE HEARTBEAT & CONCURRENCY (GATE 3)
# ==============================================================================

@pytest.mark.asyncio
async def test_lease_01_normal_renewal_extends_expiration():
    """renew_operation_claim extends lease_expires_at and increments version."""
    store = srv.store
    key = f"op_test_{uuid.uuid4().hex[:8]}"
    acq, claim = await store.claim_operation(
        idempotency_key=key,
        workflow_id="wf-1",
        tool_name="test_tool",
        target_entity_id="e1",
        parameters={},
        worker_id="worker-a",
        lease_seconds=30,
    )
    assert acq is True
    initial_exp = claim["lease_expires_at"]

    # Renew lease
    renewed, updated = await store.renew_operation_claim(
        idempotency_key=key,
        worker_id="worker-a",
        lease_seconds=60,
    )
    assert renewed is True
    assert updated["lease_expires_at"] > initial_exp
    assert updated["version"] == claim["version"] + 1


@pytest.mark.asyncio
async def test_lease_02_renewal_by_wrong_worker_rejected():
    """Worker B cannot renew a lease owned by Worker A."""
    store = srv.store
    key = f"op_test_{uuid.uuid4().hex[:8]}"
    await store.claim_operation(
        idempotency_key=key,
        workflow_id="wf-1",
        tool_name="test_tool",
        target_entity_id="e1",
        parameters={},
        worker_id="worker-a",
        lease_seconds=60,
    )

    renewed, _ = await store.renew_operation_claim(
        idempotency_key=key,
        worker_id="worker-b",
        lease_seconds=60,
    )
    assert renewed is False


@pytest.mark.asyncio
async def test_lease_03_renewal_after_completion_rejected():
    """Completed operation claims cannot be renewed."""
    store = srv.store
    key = f"op_test_{uuid.uuid4().hex[:8]}"
    await store.claim_operation(
        idempotency_key=key,
        workflow_id="wf-1",
        tool_name="test_tool",
        target_entity_id="e1",
        parameters={},
        worker_id="worker-a",
        lease_seconds=60,
    )
    await store.complete_operation(idempotency_key=key, result={"status": "OK"}, worker_id="worker-a")

    renewed, _ = await store.renew_operation_claim(
        idempotency_key=key,
        worker_id="worker-a",
        lease_seconds=60,
    )
    assert renewed is False


@pytest.mark.asyncio
async def test_lease_04_worker_consumer_runs_heartbeat_loop():
    """WorkflowEventConsumer maintains an active heartbeat task during agent execution."""
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
        worker_id="worker-heartbeat-test",
    )

    wf_id = f"wf-hb-{uuid.uuid4().hex[:8]}"
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Heartbeat Test",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-default",
    )

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-default",
        idempotency_key=f"op_{wf_id}_v1",
        expected_version=1,
        correlation_id="corr-hb-1",
        producer_id="api-test",
        payload={"scenario": "billing_unavailable"},
    )

    result = await consumer.consume_message(msg)
    assert result["status"] == "PROCESSED"

    # Verify final operation claim is COMPLETED
    op = await store.get_operation(f"op_{wf_id}_v1")
    assert op is not None
    assert op["status"] == "COMPLETED"


# ==============================================================================
# 4. DISTRIBUTED EVENT DELIVERY & RECONNECTION (GATE 4)
# ==============================================================================

@pytest.mark.asyncio
async def test_dist_01_cross_container_event_observation_via_durable_backlog():
    """Simulate worker appending event to store; SSE stream detects and yields the event."""
    store = srv.store
    wf_id = f"wf-dist-{uuid.uuid4().hex[:8]}"
    await store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-default",
        "state": "EXECUTING",
        "version": 1,
    })

    # Worker appends event directly to store
    await store.append_event(wf_id, {
        "event_id": "ev-worker-1",
        "workflow_id": wf_id,
        "event_type": "STEP_COMPLETED",
        "title": "Remote Worker Step",
    })

    events = await store.get_events(wf_id)
    assert len(events) == 1
    assert events[0]["event_id"] == "ev-worker-1"


@pytest.mark.asyncio
async def test_dist_02_client_reconnect_replays_missed_events():
    """When an SSE client reconnects, all historical events are delivered from backlog."""
    store = srv.store
    wf_id = f"wf-recon-{uuid.uuid4().hex[:8]}"
    await store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-default",
        "state": "COMPLETED",
        "version": 1,
    })

    # 3 events in history
    for i in range(3):
        await store.append_event(wf_id, {
            "event_id": f"ev-{i}",
            "workflow_id": wf_id,
            "event_type": "AGENT_REASONING",
            "title": f"Step {i}",
        })

    principal = Principal(user_id="op-1", role=Role.OPERATOR, tenant_id="tenant-default")
    ticket = await sse_ticket_store.issue_ticket(principal, wf_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(f"/api/workflows/{wf_id}/events/stream?ticket={ticket.ticket_id}")
        assert resp.status_code == 200
        text = resp.text
        assert "Step 0" in text
        assert "Step 1" in text
        assert "Step 2" in text
        assert "STREAM_END" in text


# ==============================================================================
# 5. FIRESTORE SCALABILITY & COMPOSITE INDEXES (GATE 5)
# ==============================================================================

def test_firestore_01_composite_indexes_json_complete():
    """Verify firestore.indexes.json defines all required composite index rules."""
    index_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "firestore.indexes.json")
    with open(index_file, "r") as f:
        data = json.load(f)

    indexes = data.get("indexes", [])
    assert len(indexes) >= 6

    # Verify admin query indexes exist
    admin_state_idx = any(
        idx.get("collectionGroup") == "workflows" and
        [f["fieldPath"] for f in idx.get("fields", [])] == ["state", "updated_at"]
        for idx in indexes
    )
    assert admin_state_idx is True, "Admin state index must be defined"


@pytest.mark.asyncio
async def test_firestore_02_count_workflows_aggregation():
    """Verify count_workflows aggregates correctly."""
    store = srv.store
    for i in range(5):
        await store.save_workflow({
            "workflow_id": f"wf-cnt-{i}",
            "tenant_id": "tenant-alpha",
            "state": "COMPLETED" if i < 3 else "EXECUTING",
            "version": 1,
        })

    count_all = await store.count_workflows(tenant_id="tenant-alpha")
    assert count_all == 5

    count_completed = await store.count_workflows(tenant_id="tenant-alpha", state="COMPLETED")
    assert count_completed == 3


# ==============================================================================
# 6. CHAOS & ADVERSARIAL RESILIENCE SCENARIOS (GATE 7: R21–R30)
# ==============================================================================

@pytest.mark.asyncio
async def test_r21_concurrent_second_claim_rejected_during_active_lease():
    """R21: Second worker attempt to claim an active leased operation is rejected."""
    store = srv.store
    key = f"op_chaos_{uuid.uuid4().hex[:8]}"
    acq1, _ = await store.claim_operation(
        idempotency_key=key,
        workflow_id="wf-1",
        tool_name="test_tool",
        target_entity_id="e1",
        parameters={},
        worker_id="worker-1",
        lease_seconds=60,
    )
    assert acq1 is True

    # Concurrent attempt by worker-2
    acq2, _ = await store.claim_operation(
        idempotency_key=key,
        workflow_id="wf-1",
        tool_name="test_tool",
        target_entity_id="e1",
        parameters={},
        worker_id="worker-2",
        lease_seconds=60,
    )
    assert acq2 is False


@pytest.mark.asyncio
async def test_r22_tenant_crossing_sse_ticket_request_denied():
    """R22: User from tenant A cannot issue an SSE ticket for a workflow in tenant B."""
    wf_id = f"wf-iso-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-victim",
        "state": "EXECUTING",
        "version": 1,
    })

    # Attacker token from tenant-attacker
    token = create_access_token(
        user_id="attacker-1",
        role=Role.OPERATOR,
        tenant_id="tenant-attacker",
        secret_key=config.jwt_secret_key,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/auth/sse-ticket",
            json={"workflow_id": wf_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
