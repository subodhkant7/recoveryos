"""
Phase 6.5 Operability, Observability, and Recovery Test Suite.

Verifies:
1. Operational metrics exposition on /metrics endpoint.
2. Canonical logging and structured event constants.
3. Stuck workflow diagnostic analysis (GET /api/workflows/{id}/diagnostics).
4. Operator recovery and redrive lifecycle (POST /api/workflows/{id}/recover).
5. RBAC and cross-tenant security boundaries on diagnostics and recovery.
6. Terminal state protection (COMPLETED/ESCALATED) during recovery.
7. OCC version binding and fresh idempotency keys during recovery dispatch.
"""

import dataclasses
import json
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
import pytest
from httpx import AsyncClient, ASGITransport

from backend.api.server import app, set_event_publisher, store, engine
from backend.config import config
from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
)
from backend.events.publisher import BaseEventPublisher, EventPublishError
from backend.models.workflow import WorkflowState
from backend.models.idempotency import OperationStatus
from backend.observability.logging import (
    EVENT_WORKFLOW_DISPATCHED,
    EVENT_WORKFLOW_RECOVERED,
    EVENT_WORKFLOW_OCC_MISMATCH,
)
from backend.observability.metrics import (
    metrics,
    record_workflow_dispatched,
    record_worker_execution,
    record_occ_mismatch,
    record_duplicate_claim,
    record_workflow_recovery,
)
from backend.security.tokens import create_access_token
from backend.security.principal import Role
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
import backend.api.server as srv
from backend.api.server import app, set_event_publisher


class MockRecoveryPublisher(BaseEventPublisher):
    """Test publisher tracking published messages."""

    def __init__(self, should_fail: bool = False):
        self.published_messages: list[WorkflowExecutionMessage] = []
        self.should_fail = should_fail

    async def publish_workflow_execution(self, message: WorkflowExecutionMessage) -> str:
        if self.should_fail:
            raise EventPublishError("Pub/Sub recovery dispatch failed")
        self.published_messages.append(message)
        return f"pubsub-rec-msg-{len(self.published_messages)}"


@pytest.fixture(autouse=True)
def clean_metrics():
    metrics.clear()
    yield
    metrics.clear()


@pytest.fixture
def operator_token():
    return create_access_token(
        user_id="op-test-1",
        role=Role.OPERATOR,
        tenant_id="tenant-alpha",
    )


@pytest.fixture
def viewer_token():
    return create_access_token(
        user_id="view-test-1",
        role=Role.VIEWER,
        tenant_id="tenant-alpha",
    )


@pytest.fixture
def other_tenant_token():
    return create_access_token(
        user_id="op-other-1",
        role=Role.OPERATOR,
        tenant_id="tenant-beta",
    )


async def _create_test_wf(tenant_id="tenant-alpha", state=WorkflowState.CREATED):
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    wf_data = await srv.engine.create_workflow(
        name="Phase 6.5 Operability WF",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id=tenant_id,
    )
    if state == WorkflowState.EXECUTING:
        await srv.engine.transition(wf_id, WorkflowState.EXECUTING, detail="Set test state EXECUTING")
    elif state == WorkflowState.ESCALATED:
        await srv.engine.transition(wf_id, WorkflowState.EXECUTING, detail="Step 1: EXECUTING")
        await srv.engine.transition(wf_id, WorkflowState.ESCALATED, detail="Step 2: ESCALATED")
    elif state == WorkflowState.COMPLETED:
        await srv.engine.transition(wf_id, WorkflowState.EXECUTING, detail="Step 1: EXECUTING")
        await srv.engine.transition(wf_id, WorkflowState.VERIFYING, detail="Step 2: VERIFYING")
        await srv.engine.transition(wf_id, WorkflowState.COMPLETED, detail="Step 3: COMPLETED")
    return wf_id


# ===========================================================================
# 1. Operational Metrics Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_01_metrics_endpoint_export():
    """Prometheus metrics endpoint /metrics exports registered operational counters."""
    record_workflow_dispatched(scenario="billing_unavailable", tenant_id="tenant-alpha")
    record_worker_execution(status="ACK", failure_type="none")
    record_occ_mismatch()
    record_duplicate_claim()
    record_workflow_recovery(status="dispatched")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/metrics")
        assert res.status_code == 200
        text = res.text
        assert "recoveryos_workflows_dispatched_total" in text
        assert "recoveryos_worker_executions_total" in text
        assert "recoveryos_occ_mismatches_total 1" in text
        assert "recoveryos_duplicate_claims_total 1" in text
        assert "recoveryos_recoveries_total" in text


# ===========================================================================
# 2. Stuck Workflow Diagnostic Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_02_workflow_diagnostics_healthy(operator_token):
    """GET /api/workflows/{id}/diagnostics returns operational health analysis."""
    wf_id = await _create_test_wf(tenant_id="tenant-alpha")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            f"/api/workflows/{wf_id}/diagnostics",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["workflow_id"] == wf_id
        assert data["tenant_id"] == "tenant-alpha"
        assert data["state"] == "CREATED"
        assert data["version"] == 1
        assert data["is_terminal"] is False
        assert data["is_recoverable"] is True
        assert "age_seconds" in data


@pytest.mark.asyncio
async def test_03_workflow_diagnostics_stuck_detection(operator_token):
    """Diagnostics detect stuck condition when workflow is old and in CREATED state."""
    wf_id = await _create_test_wf(tenant_id="tenant-alpha")

    # Artificially age created_at
    wf = await srv.store.get_workflow(wf_id)
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    wf["created_at"] = old_time
    await srv.store.save_workflow(wf)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            f"/api/workflows/{wf_id}/diagnostics",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["is_stuck"] is True
        assert "CREATED state for" in (data["stuck_reason"] or "")


@pytest.mark.asyncio
async def test_04_diagnostics_cross_tenant_rejected(other_tenant_token):
    """Diagnostics endpoint enforces tenant isolation (HTTP 403)."""
    wf_id = await _create_test_wf(tenant_id="tenant-alpha")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            f"/api/workflows/{wf_id}/diagnostics",
            headers={"Authorization": f"Bearer {other_tenant_token}"},
        )
        assert res.status_code == 403


# ===========================================================================
# 3. Safe Operator Recovery Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_05_operator_recovery_pubsub_dispatch(operator_token):
    """POST /api/workflows/{id}/recover dispatches RECOVERY_TRIGGER with fresh idempotency key & OCC version."""
    wf_id = await _create_test_wf(tenant_id="tenant-alpha")

    mock_pub = MockRecoveryPublisher()
    set_event_publisher(mock_pub)

    custom_cfg = dataclasses.replace(config, event_publisher_backend="pubsub")
    with patch("backend.api.server.config", custom_cfg):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(
                f"/api/workflows/{wf_id}/recover",
                json={"reason": "Operator manually redriving stuck workflow"},
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            assert res.status_code == 202
            body = res.json()
            assert body["status"] == "recovery_dispatched"
            assert body["workflow_id"] == wf_id
            assert body["current_version"] == 1
            assert "op_recover_" in body["idempotency_key"]

            # Verify published message contract
            assert len(mock_pub.published_messages) == 1
            msg = mock_pub.published_messages[0]
            assert msg.event_type == WorkflowEventType.RECOVERY_TRIGGER
            assert msg.workflow_id == wf_id
            assert msg.tenant_id == "tenant-alpha"
            assert msg.expected_version == 1

            # Verify recovery event appended to workflow audit timeline
            events = await srv.store.get_events(wf_id)
            rec_events = [e for e in events if e.get("event_type") == EVENT_WORKFLOW_RECOVERED]
            assert len(rec_events) == 1
            assert rec_events[0].get("recovered_by") == "op-test-1"


@pytest.mark.asyncio
async def test_06_recovery_viewer_role_forbidden(viewer_token):
    """User with VIEWER role cannot invoke recovery endpoint (HTTP 403)."""
    wf_id = await _create_test_wf(tenant_id="tenant-alpha")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/workflows/{wf_id}/recover",
            json={"reason": "Unauthorized recovery attempt"},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert res.status_code == 403


@pytest.mark.asyncio
async def test_07_recovery_cross_tenant_forbidden(other_tenant_token):
    """Tenant B operator cannot recover Tenant A's workflow (HTTP 403)."""
    wf_id = await _create_test_wf(tenant_id="tenant-alpha")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/workflows/{wf_id}/recover",
            json={"reason": "Cross-tenant recovery attempt"},
            headers={"Authorization": f"Bearer {other_tenant_token}"},
        )
        assert res.status_code == 403


@pytest.mark.asyncio
async def test_08_recovery_completed_workflow_rejected(operator_token):
    """Cannot recover a COMPLETED workflow (HTTP 400)."""
    wf_id = await _create_test_wf(tenant_id="tenant-alpha", state=WorkflowState.COMPLETED)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/workflows/{wf_id}/recover",
            json={"reason": "Attempting recovery on completed workflow"},
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert res.status_code == 400
        assert "Cannot recover a COMPLETED workflow" in res.json().get("detail", "")


@pytest.mark.asyncio
async def test_09_recovery_escalated_workflow_requires_force(operator_token):
    """Recovering an ESCALATED workflow requires force=true (HTTP 400 without force)."""
    wf_id = await _create_test_wf(tenant_id="tenant-alpha", state=WorkflowState.ESCALATED)

    mock_pub = MockRecoveryPublisher()
    set_event_publisher(mock_pub)

    custom_cfg = dataclasses.replace(config, event_publisher_backend="pubsub")
    with patch("backend.api.server.config", custom_cfg):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Without force
            r1 = await client.post(
                f"/api/workflows/{wf_id}/recover",
                json={"reason": "Attempting unforced recovery on escalated workflow", "force": False},
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            assert r1.status_code == 400
            assert "pass force=true" in r1.json().get("detail", "")

            # 2. With force=True
            r2 = await client.post(
                f"/api/workflows/{wf_id}/recover",
                json={"reason": "Admin authorized incident redrive", "force": True},
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            assert r2.status_code == 202
            assert r2.json()["status"] == "recovery_dispatched"
