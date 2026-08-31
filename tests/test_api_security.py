"""
Phase 5.4.3: Production API Security, Authentication & RBAC Test Suite.

Verifies:
AUTH-01 missing token -> 401
AUTH-02 malformed token -> 401
AUTH-03 expired token -> 401
AUTH-04 invalid signature -> 401
AUTH-05 valid viewer token -> allowed only for viewer permissions
AUTH-06 operator can perform operator action
AUTH-07 viewer cannot perform operator action -> 403
AUTH-08 operator cannot approve -> 403
AUTH-09 approver can approve valid pending approval
AUTH-10 admin can approve
AUTH-11 forged approved_by field cannot bypass authorization
AUTH-12 forged role in request body cannot elevate privileges
AUTH-13 approval for another workflow rejected -> 404
AUTH-14 duplicate approval rejected -> 400
AUTH-15 terminal workflow approval rejected -> 400
AUTH-16 cross-tenant workflow access rejected -> 403
AUTH-17 token/secret values never appear in logs / audit history
AUTH-18 privileged actions generate audit events
AUTH-19 invalid authorization cannot mutate workflow state
AUTH-20 existing PolicyEngine approval invariants remain enforced
"""

import uuid
import pytest
from datetime import datetime, timezone, timedelta
from httpx import ASGITransport, AsyncClient

import backend.api.server as srv
from backend.api.server import app
from backend.models.workflow import WorkflowState
from backend.security.principal import Role
from backend.security.tokens import create_access_token
from backend.security.audit import get_security_audit_logs, clear_security_audit_logs


@pytest.fixture(autouse=True)
def setup_security():
    clear_security_audit_logs()


@pytest.fixture
def viewer_token():
    return create_access_token(user_id="user_viewer", role=Role.VIEWER, tenant_id="tenant-acme")


@pytest.fixture
def operator_token():
    return create_access_token(user_id="user_operator", role=Role.OPERATOR, tenant_id="tenant-acme")


@pytest.fixture
def approver_token():
    return create_access_token(user_id="user_approver", role=Role.APPROVER, tenant_id="tenant-acme")


@pytest.fixture
def admin_token():
    return create_access_token(user_id="user_admin", role=Role.ADMIN, tenant_id="tenant-acme")


@pytest.fixture
def other_tenant_token():
    return create_access_token(user_id="user_other", role=Role.OPERATOR, tenant_id="tenant-other")


@pytest.mark.asyncio
async def test_auth_01_missing_token():
    """AUTH-01: Missing token -> HTTP 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/workflows")
        assert resp.status_code == 401
        assert "Authentication required" in resp.text


@pytest.mark.asyncio
async def test_auth_02_malformed_token():
    """AUTH-02: Malformed token -> HTTP 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer not-a-valid-jwt-token"}
        resp = await client.get("/api/workflows", headers=headers)
        assert resp.status_code == 401
        assert "Invalid or expired" in resp.text


@pytest.mark.asyncio
async def test_auth_03_expired_token():
    """AUTH-03: Expired token -> HTTP 401."""
    expired_token = create_access_token(
        user_id="user_expired",
        role=Role.OPERATOR,
        expires_delta=timedelta(seconds=-10),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {expired_token}"}
        resp = await client.get("/api/workflows", headers=headers)
        assert resp.status_code == 401
        assert "Invalid or expired" in resp.text


@pytest.mark.asyncio
async def test_auth_04_invalid_signature():
    """AUTH-04: Invalid signature -> HTTP 401."""
    bad_sig_token = create_access_token(
        user_id="user_hacker",
        role=Role.ADMIN,
        secret_key="completely-wrong-secret-key-attacker",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {bad_sig_token}"}
        resp = await client.get("/api/workflows", headers=headers)
        assert resp.status_code == 401
        assert "Invalid or expired" in resp.text


@pytest.mark.asyncio
async def test_auth_05_valid_viewer_token_allowed_read(viewer_token):
    """AUTH-05: Valid viewer token -> allowed for read endpoints."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {viewer_token}"}
        resp = await client.get("/api/workflows", headers=headers)
        assert resp.status_code == 200
        assert "workflows" in resp.json()


@pytest.mark.asyncio
async def test_auth_06_operator_can_launch_scenario(operator_token):
    """AUTH-06: Operator can perform operator action (launch scenario)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {operator_token}"}
        resp = await client.post("/api/scenarios/billing_unavailable", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "launched"
        assert data["tenant_id"] == "tenant-acme"


@pytest.mark.asyncio
async def test_auth_07_viewer_cannot_launch_scenario(viewer_token):
    """AUTH-07: Viewer cannot perform operator action -> HTTP 403."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {viewer_token}"}
        resp = await client.post("/api/scenarios/billing_unavailable", headers=headers)
        assert resp.status_code == 403
        assert "Insufficient permissions" in resp.text


@pytest.mark.asyncio
async def test_auth_08_operator_cannot_approve(operator_token):
    """AUTH-08: Operator cannot approve approval-gated actions -> HTTP 403."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "paypal"},
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {operator_token}"}
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Operator trying to self-approve"},
            headers=headers,
        )
        assert resp.status_code == 403
        assert "Insufficient permissions" in resp.text


@pytest.mark.asyncio
async def test_auth_09_approver_can_approve(approver_token):
    """AUTH-09: Approver can approve valid pending approval."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "paypal"},
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {approver_token}"}
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Risk accepted by approver"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "decided"
        assert data["approved"] is True
        assert data["decided_by"] == "user_approver"

    # Verify stored approval has authoritative user_id
    stored_appr = await srv.store.get_approval(wf_id, appr_id)
    assert stored_appr["status"] == "APPROVED"
    assert stored_appr["decided_by"] == "user_approver"


@pytest.mark.asyncio
async def test_auth_10_admin_can_approve(admin_token):
    """AUTH-10: Admin can approve pending approvals."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "paypal"},
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {admin_token}"}
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Admin override"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["decided_by"] == "user_admin"


@pytest.mark.asyncio
async def test_auth_11_forged_approved_by_overridden_by_token(approver_token):
    """AUTH-11: Client-supplied forged decided_by is overridden by verified principal user_id."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "paypal"},
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {approver_token}"}
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Valid reason", "decided_by": "spoofed_ceo_account"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["decided_by"] == "user_approver"

    stored_appr = await srv.store.get_approval(wf_id, appr_id)
    assert stored_appr["decided_by"] == "user_approver"
    assert stored_appr["decided_by"] != "spoofed_ceo_account"


@pytest.mark.asyncio
async def test_auth_12_forged_role_in_body_cannot_elevate(viewer_token):
    """AUTH-12: Forged role in request body cannot bypass RBAC."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {viewer_token}"}
        resp = await client.post(
            "/api/scenarios/billing_unavailable",
            json={"role": "admin", "bypass_auth": True},
            headers=headers,
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_auth_13_approval_for_wrong_workflow_rejected(approver_token):
    """AUTH-13: Approval ID not belonging to specified workflow -> HTTP 404."""
    wf1_id = str(uuid.uuid4())
    wf2_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"

    await srv.store.save_workflow({"workflow_id": wf1_id, "tenant_id": "tenant-acme", "state": "AWAITING_APPROVAL", "version": 1})
    await srv.store.save_workflow({"workflow_id": wf2_id, "tenant_id": "tenant-acme", "state": "AWAITING_APPROVAL", "version": 1})
    # Save approval on wf1
    await srv.store.save_approval(wf1_id, {"approval_id": appr_id, "workflow_id": wf1_id, "status": "PENDING"})

    # Attempt to approve under wf2
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {approver_token}"}
        resp = await client.post(
            f"/api/workflows/{wf2_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Mismatched workflow test"},
            headers=headers,
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_auth_14_duplicate_approval_rejected(approver_token):
    """AUTH-14: Already decided approval cannot be decided twice -> HTTP 400."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({"workflow_id": wf_id, "tenant_id": "tenant-acme", "state": "AWAITING_APPROVAL", "version": 1})
    await srv.store.save_approval(wf_id, {"approval_id": appr_id, "workflow_id": wf_id, "status": "PENDING"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {approver_token}"}
        # First decision succeeds
        resp1 = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "First decision"},
            headers=headers,
        )
        assert resp1.status_code == 200

        # Second duplicate decision fails
        resp2 = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": False, "reason": "Second duplicate decision"},
            headers=headers,
        )
        assert resp2.status_code == 400
        assert "already decided" in resp2.text


@pytest.mark.asyncio
async def test_auth_15_terminal_workflow_approval_rejected(approver_token):
    """AUTH-15: Approving a workflow in terminal state (COMPLETED/ESCALATED) -> HTTP 400."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({"workflow_id": wf_id, "tenant_id": "tenant-acme", "state": "ESCALATED", "version": 1})
    await srv.store.save_approval(wf_id, {"approval_id": appr_id, "workflow_id": wf_id, "status": "PENDING"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {approver_token}"}
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Trying to approve terminal workflow"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "terminal workflow" in resp.text


@pytest.mark.asyncio
async def test_auth_16_cross_tenant_access_rejected(other_tenant_token):
    """AUTH-16: User from tenant-other cannot access tenant-acme workflow -> HTTP 403."""
    wf_id = str(uuid.uuid4())
    await srv.store.save_workflow({"workflow_id": wf_id, "tenant_id": "tenant-acme", "state": "EXECUTING", "version": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {other_tenant_token}"}
        resp = await client.get(f"/api/workflows/{wf_id}", headers=headers)
        assert resp.status_code == 403
        assert "Cross-tenant" in resp.text


@pytest.mark.asyncio
async def test_auth_17_tokens_never_logged_in_audit():
    """AUTH-17: Token/secret values never appear in security audit logs."""
    bad_token = "secret-token-value-must-not-appear-in-logs"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {bad_token}"}
        await client.get("/api/workflows", headers=headers)

    logs = get_security_audit_logs()
    assert len(logs) >= 1
    for log in logs:
        log_str = str(log)
        assert bad_token not in log_str
        assert "secret-token-value" not in log_str


@pytest.mark.asyncio
async def test_auth_18_privileged_actions_generate_audit_events(operator_token):
    """AUTH-18: Privileged operations produce structured security audit records."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {operator_token}"}
        resp = await client.post("/api/scenarios/billing_unavailable", headers=headers)
        assert resp.status_code == 200

    logs = get_security_audit_logs()
    mutation_events = [l for l in logs if l.get("event_type") == "PRIVILEGED_MUTATION"]
    assert len(mutation_events) >= 1
    assert mutation_events[0]["actor_id"] == "user_operator"
    assert mutation_events[0]["action"] == "launch_scenario"


@pytest.mark.asyncio
async def test_auth_19_invalid_authorization_cannot_mutate_workflow(viewer_token):
    """AUTH-19: Unauthorized request cannot mutate workflow state."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {"approval_id": appr_id, "workflow_id": wf_id, "status": "PENDING"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {viewer_token}"}
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Viewer trying to approve"},
            headers=headers,
        )
        assert resp.status_code == 403

    wf_after = await srv.store.get_workflow(wf_id)
    assert wf_after["state"] == WorkflowState.AWAITING_APPROVAL.value
    appr_after = await srv.store.get_approval(wf_id, appr_id)
    assert appr_after["status"] == "PENDING"


@pytest.mark.asyncio
async def test_auth_20_policy_engine_invariants_preserved():
    """AUTH-20: Existing PolicyEngine security boundaries and rule evaluations remain intact."""
    from backend.engine.policy_engine import PolicyEngine
    from backend.models.policy import PolicyOutcome
    from backend.simulation.scenarios import create_acme_contract
    pe = PolicyEngine()
    contract = create_acme_contract("wf-test-20")
    workflow_state = {"workflow_id": "wf-test-20", "state": "EXECUTING"}
    # Attempting setup_billing without identity verification evidence must trigger PolicyOutcome.REJECTED
    eval_result = pe.evaluate(
        tool_name="setup_billing",
        tool_args={"customer_id": "acme-001", "provider": "paypal"},
        workflow_state=workflow_state,
        evidence=[],
        contract=contract,
    )
    assert eval_result.outcome == PolicyOutcome.REJECTED


@pytest.mark.asyncio
async def test_authorized_same_tenant_can_approve(approver_token):
    """Authorized approver in the same tenant can approve pending workflow."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "paypal"},
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {approver_token}"}
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Authorized human decision"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "decided"
        assert data["approved"] is True
        assert data["decided_by"] == "user_approver"

    appr = await srv.store.get_approval(wf_id, appr_id)
    assert appr["status"] == "APPROVED"
    assert appr["decided_by"] == "user_approver"


@pytest.mark.asyncio
async def test_authorized_same_tenant_can_reject(approver_token):
    """Authorized approver in the same tenant can reject pending workflow and escalate."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "paypal"},
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {approver_token}"}
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": False, "reason": "Operator rejected risky action"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "decided"
        assert data["approved"] is False
        assert data["decided_by"] == "user_approver"

    wf = await srv.store.get_workflow(wf_id)
    assert wf["state"] == WorkflowState.ESCALATED.value
    appr = await srv.store.get_approval(wf_id, appr_id)
    assert appr["status"] == "REJECTED"


@pytest.mark.asyncio
async def test_unauthorized_role_cannot_approve(operator_token, viewer_token):
    """Operators and Viewers cannot submit approvals."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Operator attempt
        resp_op = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True},
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert resp_op.status_code == 403
        assert "Insufficient permissions" in resp_op.text

        # Viewer attempt
        resp_vi = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert resp_vi.status_code == 403
        assert "Insufficient permissions" in resp_vi.text


@pytest.mark.asyncio
async def test_cross_tenant_approval_is_denied():
    """Approver from a different tenant is denied approval on another tenant's workflow."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
    })

    other_tenant_approver = create_access_token(
        user_id="user_approver_corp",
        role=Role.APPROVER,
        tenant_id="tenant-corp",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Malicious cross-tenant attempt"},
            headers={"Authorization": f"Bearer {other_tenant_approver}"},
        )
        assert resp.status_code == 403
        assert "Cross-tenant access forbidden" in resp.text


@pytest.mark.asyncio
async def test_approval_does_not_bypass_verification(approver_token):
    """Approving an action does not automatically mark the outcome verified."""
    wf_id = str(uuid.uuid4())
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    contract = {
        "workflow_id": wf_id,
        "required_outcomes": [
            {
                "outcome_id": "billing_configured",
                "description": "Billing active",
                "acceptance_criteria": {"plan_tier": "enterprise"},
                "verification_method": "query_billing_service",
                "required_evidence": ["subscription_id"],
                "status": "UNVERIFIED",
            }
        ],
        "constraints": [],
        "prohibited_outcomes": [],
    }
    await srv.store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "contract": contract,
        "version": 1,
    })
    await srv.store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
        "action_tool": "setup_billing",
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/workflows/{wf_id}/approve/{appr_id}",
            json={"approved": True, "reason": "Approved by human"},
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 200

    wf = await srv.store.get_workflow(wf_id)
    # Outcome must remain UNVERIFIED until independent verification executes
    outcomes = wf["contract"]["required_outcomes"]
    assert outcomes[0]["status"] == "UNVERIFIED"
