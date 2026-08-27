"""
Phase 9 Automated Test Suite: Operator Control Plane & Recovery Console.

Covers:
1. Filtered & Paginated Workflow Discovery (limit, offset, state, scenario, stuck filter, search).
2. Multi-Tenant Isolation on Workflow Queries (Tenant A vs Tenant B vs Admin).
3. Fleet Health Overview Endpoint (GET /api/operator/overview).
4. Stuck Workflow Aggregation (GET /api/operator/stuck-workflows).
5. Operator Workflow Cancellation (POST /api/workflows/{id}/cancel) + Terminal Guards.
6. Persistent Security Audit Trail (GET /api/audit/logs) + Role Gating.
7. Operator Console UI Static Asset Mounting (/console).
"""

from __future__ import annotations

import asyncio
import uuid
import pytest
from httpx import AsyncClient, ASGITransport

import backend.api.server as srv
from backend.api.server import app
from backend.models.workflow import WorkflowState
from backend.security.tokens import create_access_token
from backend.config import config
from backend.security.audit import clear_security_audit_logs
from backend.observability.logging import current_request_id, current_workflow_id, current_tenant_id


@pytest.fixture(autouse=True)
def _clean_test_store():
    """Ensure in-memory store and context are clean between tests."""
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


def _make_token(user_id: str, role: str, tenant_id: str = "tenant-test") -> str:
    return create_access_token(
        user_id=user_id,
        role=role,
        tenant_id=tenant_id,
        secret_key=config.jwt_secret_key,
    )


async def _seed_workflow(
    wf_id: str,
    tenant_id: str = "tenant-test",
    state: WorkflowState = WorkflowState.CREATED,
    scenario: str = "billing_unavailable",
    company_name: str = "Acme Corp",
    age_seconds: float = 0.0,
):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    created_dt = now - timedelta(seconds=age_seconds)
    data = {
        "workflow_id": wf_id,
        "tenant_id": tenant_id,
        "name": f"Workflow {wf_id}",
        "scenario": scenario,
        "state": state.value,
        "version": 1,
        "customer_data": {"company_name": company_name},
        "created_at": created_dt.isoformat(),
        "updated_at": created_dt.isoformat(),
    }
    await srv.store.save_workflow(data)
    return data


# ===========================================================================
# 1. Filtered & Paginated Workflow Discovery
# ===========================================================================

@pytest.mark.asyncio
async def test_01_workflows_pagination_limit_and_offset():
    """Test limit and offset pagination on GET /api/workflows."""
    for i in range(15):
        await _seed_workflow(f"wf-page-{i:02d}", tenant_id="tenant-test")

    token = _make_token("op-1", "operator", "tenant-test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Page 1 (limit=5, offset=0)
        resp1 = await client.get("/api/workflows?limit=5&offset=0", headers={"Authorization": f"Bearer {token}"})
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert len(data1["workflows"]) == 5
        assert data1["total"] == 15
        assert data1["offset"] == 0

        # Page 2 (limit=5, offset=5)
        resp2 = await client.get("/api/workflows?limit=5&offset=5", headers={"Authorization": f"Bearer {token}"})
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert len(data2["workflows"]) == 5
        assert data2["offset"] == 5

        # Verify items on Page 1 and Page 2 do not overlap
        ids1 = {w["workflow_id"] for w in data1["workflows"]}
        ids2 = {w["workflow_id"] for w in data2["workflows"]}
        assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_02_workflows_filter_by_state_and_scenario():
    """Test state and scenario filtering on GET /api/workflows."""
    await _seed_workflow("wf-state-1", state=WorkflowState.CREATED, scenario="billing_unavailable")
    await _seed_workflow("wf-state-2", state=WorkflowState.EXECUTING, scenario="billing_unavailable")
    await _seed_workflow("wf-state-3", state=WorkflowState.COMPLETED, scenario="identity_service_down")

    token = _make_token("op-1", "operator", "tenant-test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Filter state=EXECUTING
        resp = await client.get("/api/workflows?state=EXECUTING", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["workflows"]) == 1
        assert data["workflows"][0]["workflow_id"] == "wf-state-2"

        # Filter scenario=identity_service_down
        resp_scen = await client.get("/api/workflows?scenario=identity_service_down", headers={"Authorization": f"Bearer {token}"})
        assert resp_scen.status_code == 200
        data_scen = resp_scen.json()
        assert len(data_scen["workflows"]) == 1
        assert data_scen["workflows"][0]["workflow_id"] == "wf-state-3"


@pytest.mark.asyncio
async def test_03_workflows_text_search():
    """Test text search matching workflow_id or customer company name."""
    await _seed_workflow("wf-alpha-123", company_name="Cyberdyne Systems")
    await _seed_workflow("wf-beta-456", company_name="Weyland-Yutani")

    token = _make_token("op-1", "operator", "tenant-test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Search by company name keyword
        resp = await client.get("/api/workflows?search=Cyberdyne", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["workflows"]) == 1
        assert data["workflows"][0]["workflow_id"] == "wf-alpha-123"

        # Search by partial workflow_id
        resp_id = await client.get("/api/workflows?search=beta-456", headers={"Authorization": f"Bearer {token}"})
        assert resp_id.status_code == 200
        assert len(resp_id.json()["workflows"]) == 1


# ===========================================================================
# 2. Multi-Tenant Isolation
# ===========================================================================

@pytest.mark.asyncio
async def test_04_workflows_multi_tenant_isolation():
    """Tenant A cannot see Tenant B workflows; Admin sees all."""
    await _seed_workflow("wf-tenant-a-1", tenant_id="tenant-alpha")
    await _seed_workflow("wf-tenant-b-1", tenant_id="tenant-beta")

    token_alpha = _make_token("op-alpha", "operator", "tenant-alpha")
    token_admin = _make_token("admin-user", "admin", "tenant-default")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Operator in Tenant Alpha should only see Tenant Alpha
        resp_a = await client.get("/api/workflows", headers={"Authorization": f"Bearer {token_alpha}"})
        assert resp_a.status_code == 200
        assert len(resp_a.json()["workflows"]) == 1
        assert resp_a.json()["workflows"][0]["workflow_id"] == "wf-tenant-a-1"

        # Admin should see all tenants
        resp_admin = await client.get("/api/workflows", headers={"Authorization": f"Bearer {token_admin}"})
        assert resp_admin.status_code == 200
        assert len(resp_admin.json()["workflows"]) == 2


# ===========================================================================
# 3. Fleet Health Overview Endpoint
# ===========================================================================

@pytest.mark.asyncio
async def test_05_operator_overview_metrics():
    """Test GET /api/operator/overview aggregates states, stuck counts, and approvals."""
    await _seed_workflow("wf-ov-1", state=WorkflowState.CREATED, age_seconds=120.0)  # Stuck (CREATED > 60s)
    await _seed_workflow("wf-ov-2", state=WorkflowState.EXECUTING, age_seconds=10.0)  # Healthy
    await _seed_workflow("wf-ov-3", state=WorkflowState.COMPLETED)
    await _seed_workflow("wf-ov-4", state=WorkflowState.AWAITING_APPROVAL)

    # Add a pending approval for wf-ov-4
    await srv.store.save_approval("wf-ov-4", {
        "approval_id": "appr-1",
        "workflow_id": "wf-ov-4",
        "status": "PENDING",
        "plan_description": "Test Plan",
    })

    token = _make_token("op-1", "operator", "tenant-test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/operator/overview", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_workflows"] == 4
        assert data["counts_by_state"]["CREATED"] == 1
        assert data["counts_by_state"]["EXECUTING"] == 1
        assert data["counts_by_state"]["COMPLETED"] == 1
        assert data["counts_by_state"]["AWAITING_APPROVAL"] == 1
        assert data["stuck_count"] == 1  # wf-ov-1 is stuck
        assert data["pending_approvals_count"] == 1
        assert data["system_status"] == "HEALTHY"


# ===========================================================================
# 4. Stuck Workflow Aggregator
# ===========================================================================

@pytest.mark.asyncio
async def test_06_operator_stuck_workflows_aggregation():
    """Test GET /api/operator/stuck-workflows returns all stuck workflows with reasons."""
    await _seed_workflow("wf-stuck-1", state=WorkflowState.CREATED, age_seconds=90.0)
    await _seed_workflow("wf-healthy-1", state=WorkflowState.EXECUTING, age_seconds=5.0)

    token = _make_token("op-1", "operator", "tenant-test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/operator/stuck-workflows", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["stuck_workflows"]) == 1
        stuck_item = data["stuck_workflows"][0]
        assert stuck_item["workflow_id"] == "wf-stuck-1"
        assert stuck_item["is_stuck"] is True
        assert "CREATED" in stuck_item["stuck_reason"]


# ===========================================================================
# 5. Operator Workflow Cancellation & Terminal Guards
# ===========================================================================

@pytest.mark.asyncio
async def test_07_workflow_cancellation_and_terminal_guard():
    """POST /api/workflows/{id}/cancel transitions to ESCALATED and rejects COMPLETED."""
    await _seed_workflow("wf-cancel-target", state=WorkflowState.EXECUTING)
    await _seed_workflow("wf-completed-target", state=WorkflowState.COMPLETED)

    token = _make_token("op-1", "operator", "tenant-test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Cancel executing workflow
        resp = await client.post(
            "/api/workflows/wf-cancel-target/cancel",
            headers={"Authorization": f"Bearer {token}"},
            json={"reason": "Operator abort requested for test"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "workflow_cancelled"
        assert resp.json()["state"] == WorkflowState.ESCALATED.value

        # Verify state in store
        wf = await srv.store.get_workflow("wf-cancel-target")
        assert wf["state"] == WorkflowState.ESCALATED.value

        # Attempt to cancel COMPLETED workflow (must fail with 400)
        resp_term = await client.post(
            "/api/workflows/wf-completed-target/cancel",
            headers={"Authorization": f"Bearer {token}"},
            json={"reason": "Cannot cancel completed"},
        )
        assert resp_term.status_code == 400


@pytest.mark.asyncio
async def test_08_cancellation_role_gating():
    """Viewer role cannot cancel workflows; Operator and Admin can."""
    await _seed_workflow("wf-role-guard", state=WorkflowState.EXECUTING)

    viewer_token = _make_token("viewer-1", "viewer", "tenant-test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/workflows/wf-role-guard/cancel",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={"reason": "Viewer unauthorized attempt"},
        )
        assert resp.status_code == 403


# ===========================================================================
# 6. Persistent Security Audit Trail
# ===========================================================================

@pytest.mark.asyncio
async def test_09_audit_logs_query_and_role_gating():
    """Test GET /api/audit/logs returns persistent records with role gating."""
    # Seed audit records in store
    await srv.store.save_audit_event({
        "audit_id": "aud-1",
        "timestamp": "2026-08-27T10:00:00Z",
        "event_type": "PRIVILEGED_MUTATION",
        "actor_id": "operator-1",
        "role": "operator",
        "tenant_id": "tenant-test",
        "workflow_id": "wf-1",
        "action": "recover_workflow",
        "outcome": "ALLOWED",
        "reason": "Test recovery",
    })

    viewer_token = _make_token("viewer-1", "viewer", "tenant-test")
    operator_token = _make_token("op-1", "operator", "tenant-test")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Viewer denied (403)
        resp_view = await client.get("/api/audit/logs", headers={"Authorization": f"Bearer {viewer_token}"})
        assert resp_view.status_code == 403

        # Operator allowed (200) - includes seeded event plus the auto-recorded SECURITY_DENIED event
        resp_op = await client.get("/api/audit/logs", headers={"Authorization": f"Bearer {operator_token}"})
        assert resp_op.status_code == 200
        data = resp_op.json()
        assert data["total"] >= 1
        audit_ids = [l.get("audit_id") for l in data["audit_logs"]]
        assert "aud-1" in audit_ids

        # Filter by event_type
        resp_filtered = await client.get(
            "/api/audit/logs?event_type=PRIVILEGED_MUTATION",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert resp_filtered.status_code == 200
        assert resp_filtered.json()["total"] == 1
        assert resp_filtered.json()["audit_logs"][0]["audit_id"] == "aud-1"


# ===========================================================================
# 7. Operator Console UI Static Asset Mounting
# ===========================================================================

@pytest.mark.asyncio
async def test_10_console_static_asset_serving():
    """Verify static assets are correctly served at /console."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check index.html
        resp_html = await client.get("/console/index.html")
        assert resp_html.status_code == 200
        assert "RecoveryOS — Operator Control Plane" in resp_html.text

        # Check styles.css
        resp_css = await client.get("/console/styles.css")
        assert resp_css.status_code == 200
        assert "--bg-app" in resp_css.text

        # Check app.js
        resp_js = await client.get("/console/app.js")
        assert resp_js.status_code == 200
        assert "loadFleetOverview" in resp_js.text
