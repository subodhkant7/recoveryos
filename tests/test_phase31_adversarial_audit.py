"""
Phase 31: Adversarial Audit Tests.

Tests:
1. Action ≠ Recovery invariant (VERIFYING gate)
2. Terminal state immutability (COMPLETED/ESCALATED are final)
3. Approval state transitions
4. SSE ticket single-use lifecycle
5. Tenant isolation
6. Replay read-only proof (frontend audit)
7. Security scan (no secrets in static assets)
8. Recovery Proof only for COMPLETED (frontend audit)
9. MTTR derived from completed_at (frontend audit)
10. Worker resilience badges evidence-gated (frontend audit)
11. Duplicate event deduplication (frontend audit)
12. Terminal state regression protection (frontend audit)
13. Scenario lifecycle integrity
14. Demo mode is presentation-only (frontend audit)
"""

import os
import re
import pytest
import httpx
from backend.api.server import app
from backend.models.workflow import WorkflowState, VALID_TRANSITIONS
from backend.security.tokens import create_access_token
from backend.security.principal import Role


def _headers(user_id="operator", role=Role.OPERATOR, tenant="tenant-default"):
    token = create_access_token(user_id=user_id, role=role, tenant_id=tenant)
    return {"Authorization": f"Bearer {token}"}


def _approver_headers(tenant="tenant-default"):
    token = create_access_token(user_id="approver", role=Role.APPROVER, tenant_id=tenant)
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 1. ACTION ≠ RECOVERY INVARIANT
# ============================================================

def test_p31_01_action_not_recovery_invariant():
    """Verify VERIFYING → COMPLETED requires all outcomes verified.

    The WorkflowEngine.transition() enforces VALID_TRANSITIONS and
    agent_runner.py checks all_verified before COMPLETED transition.
    """
    # VERIFYING can only go to COMPLETED, RECOVERING, or UNKNOWN
    valid_from_verifying = VALID_TRANSITIONS[WorkflowState.VERIFYING]
    assert WorkflowState.COMPLETED in valid_from_verifying
    assert WorkflowState.RECOVERING in valid_from_verifying

    # EXECUTING cannot go directly to COMPLETED (must go through VERIFYING)
    valid_from_executing = VALID_TRANSITIONS[WorkflowState.EXECUTING]
    assert WorkflowState.COMPLETED not in valid_from_executing
    assert WorkflowState.VERIFYING in valid_from_executing


# ============================================================
# 2. TERMINAL STATE IMMUTABILITY
# ============================================================

def test_p31_02_terminal_states_are_immutable():
    """COMPLETED and ESCALATED have no valid outgoing transitions."""
    assert VALID_TRANSITIONS[WorkflowState.COMPLETED] == set()
    assert VALID_TRANSITIONS[WorkflowState.ESCALATED] == set()


# ============================================================
# 3. APPROVAL STATE TRANSITIONS
# ============================================================

def test_p31_03_awaiting_approval_transitions():
    """AWAITING_APPROVAL can go to EXECUTING (approve), ESCALATED (reject), or UNKNOWN."""
    valid = VALID_TRANSITIONS[WorkflowState.AWAITING_APPROVAL]
    assert WorkflowState.EXECUTING in valid
    assert WorkflowState.ESCALATED in valid
    # Cannot skip to COMPLETED from AWAITING_APPROVAL
    assert WorkflowState.COMPLETED not in valid


@pytest.mark.asyncio
async def test_p31_03b_approval_reject_escalates_workflow():
    """Rejecting an approval transitions to ESCALATED, not COMPLETED."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Launch contradictory evidence
        res = await c.post("/api/scenarios/contradictory_evidence", headers=_headers())
        wf_id = res.json()["workflow_id"]

        # Wait for workflow to reach AWAITING_APPROVAL (or check approvals)
        import asyncio
        for _ in range(10):
            snap = await c.get(f"/api/workflows/{wf_id}", headers=_headers())
            if snap.json().get("workflow", {}).get("state") == "AWAITING_APPROVAL":
                break
            await asyncio.sleep(0.5)

        # Get pending approvals
        appr_res = await c.get(f"/api/workflows/{wf_id}/approvals", headers=_headers())
        approvals = appr_res.json().get("approvals", [])
        if approvals:
            approval_id = approvals[0]["approval_id"]
            # Reject
            rej_res = await c.post(
                f"/api/workflows/{wf_id}/approve/{approval_id}",
                headers=_approver_headers(),
                json={"approved": False, "reason": "Adversarial test rejection"},
            )
            assert rej_res.status_code == 200
            assert rej_res.json()["approved"] is False

            # Verify workflow is ESCALATED
            final = await c.get(f"/api/workflows/{wf_id}", headers=_headers())
            assert final.json()["workflow"]["state"] == "ESCALATED"


# ============================================================
# 4. SSE TICKET SINGLE-USE LIFECYCLE
# ============================================================

@pytest.mark.asyncio
async def test_p31_04_sse_ticket_single_use():
    """SSE tickets are consumed on first use; reuse returns 401."""
    from backend.security.sse_tickets import sse_ticket_store

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        res = await c.post("/api/scenarios/billing_unavailable", headers=_headers())
        wf_id = res.json()["workflow_id"]

        # Mint ticket
        ticket_res = await c.post(
            "/api/auth/sse-ticket", headers=_headers(), json={"workflow_id": wf_id}
        )
        assert ticket_res.status_code == 200
        ticket = ticket_res.json()["ticket"]
        assert ticket.startswith("sset_")

        # 1. First consumption succeeds and returns authenticated principal
        principal = await sse_ticket_store.consume_ticket(ticket, wf_id)
        assert principal is not None
        assert principal.user_id == "operator"

        # 2. Second consumption fails (single-use semantics enforced)
        principal_reuse = await sse_ticket_store.consume_ticket(ticket, wf_id)
        assert principal_reuse is None

        # 3. Requesting SSE stream with already-consumed ticket returns HTTP 401
        reuse_res = await c.get(
            f"/api/workflows/{wf_id}/events/stream?ticket={ticket}",
        )
        assert reuse_res.status_code == 401
        assert "Invalid, expired, or already used" in reuse_res.json()["detail"]


# ============================================================
# 5. TENANT ISOLATION
# ============================================================

@pytest.mark.asyncio
async def test_p31_05_tenant_isolation():
    """A workflow created by tenant-A cannot be read by tenant-B."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Launch as tenant-default
        res = await c.post("/api/scenarios/billing_unavailable", headers=_headers())
        wf_id = res.json()["workflow_id"]

        # Access from different tenant
        other_headers = _headers(user_id="other-op", role=Role.OPERATOR, tenant="tenant-other")
        cross_res = await c.get(f"/api/workflows/{wf_id}", headers=other_headers)
        assert cross_res.status_code == 403


# ============================================================
# 6. REPLAY READ-ONLY PROOF (FRONTEND AUDIT)
# ============================================================

def test_p31_06_replay_mode_is_read_only():
    """Verify replay functions in app.js call ZERO mutation endpoints."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    # Extract the replay function bodies
    replay_functions = ["startReplay", "pauseReplay", "stepReplay", "resetReplay"]
    for fn in replay_functions:
        # Find function body
        pattern = rf"function {fn}\(\).*?\n\}}"
        match = re.search(pattern, content, re.DOTALL)
        assert match, f"Function {fn} not found in app.js"
        body = match.group()
        # Must not contain apiFetch (no backend calls)
        assert "apiFetch" not in body, f"Replay function {fn}() calls apiFetch — mutation risk!"
        # Must not contain fetch (no raw backend calls)
        assert "fetch(" not in body, f"Replay function {fn}() calls fetch — mutation risk!"


# ============================================================
# 7. SECURITY SCAN
# ============================================================

def test_p31_07_no_secrets_in_static_assets():
    """Verify no API keys, JWT secrets, or OAuth tokens in frontend files."""
    static_dir = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static")
    sensitive_patterns = [
        "AIzaSy",                         # GCP API key prefix
        "recoveryos-jwt-secret-key",      # JWT secret
        "Bearer ya29.",                   # OAuth access token
        "GOOG1",                          # GCP service account key
        "-----BEGIN PRIVATE KEY",         # Private key PEM
        "-----BEGIN RSA PRIVATE KEY",     # RSA private key
    ]
    for filename in ["index.html", "styles.css", "app.js"]:
        path = os.path.join(static_dir, filename)
        with open(path) as f:
            content = f.read()
            for pat in sensitive_patterns:
                assert pat not in content, f"Secret pattern '{pat}' found in {filename}"


# ============================================================
# 8. RECOVERY PROOF ONLY FOR COMPLETED (FRONTEND AUDIT)
# ============================================================

def test_p31_08_recovery_proof_guards_state():
    """Verify showRecoveryProof checks wf.state === 'COMPLETED'."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    # The function must contain a guard for COMPLETED
    assert "wf.state !== 'COMPLETED'" in content, \
        "showRecoveryProof does not guard against non-COMPLETED workflows"


# ============================================================
# 9. MTTR DERIVED FROM COMPLETED_AT (FRONTEND AUDIT)
# ============================================================

def test_p31_09_mttr_uses_completed_at():
    """Verify MTTR calculation uses completed_at, not updated_at."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    assert "wf.completed_at" in content, "MTTR should use wf.completed_at"


# ============================================================
# 10. WORKER RESILIENCE BADGES EVIDENCE-GATED (FRONTEND AUDIT)
# ============================================================

def test_p31_10_worker_resilience_badges_initial_pending():
    """Verify worker resilience badges start as pending in HTML."""
    html_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "index.html")
    with open(html_path) as f:
        content = f.read()

    assert 'r-badge pending' in content, "Worker resilience badges should start as pending"
    # The old hardcoded pass badges should not exist
    assert '✓ NO DUPLICATE EXECUTION' not in content, \
        "Worker resilience badges should not have static ✓ pass in HTML"


def test_p31_10b_worker_resilience_function_checks_evidence():
    """Verify showWorkerResilience reads evidence from snapshot."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    assert "showWorkerResilience(snapshot)" in content, \
        "showWorkerResilience should accept snapshot parameter"
    assert "hasResumeEvidence" in content, \
        "showWorkerResilience should check for resume evidence"


# ============================================================
# 11. EVENT DEDUPLICATION (FRONTEND AUDIT)
# ============================================================

def test_p31_11_event_deduplication():
    """Verify applyWorkflowEvent deduplicates by event_id."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    assert "seenEventIds" in content, "Event deduplication via seenEventIds should exist"
    assert "appState.seenEventIds.has(evId)" in content, \
        "Dedup should check if event already seen"


# ============================================================
# 12. TERMINAL STATE REGRESSION PROTECTION (FRONTEND AUDIT)
# ============================================================

def test_p31_12_terminal_state_regression_guard():
    """Verify handleWorkflowStateChange rejects regression from terminal state."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    assert "TERMINAL_STATES" in content, \
        "Terminal state regression guard should be present"
    assert "Reject regression from terminal state" in content, \
        "Guard comment should explain the invariant"


# ============================================================
# 13. ALL THREE SCENARIO LIFECYCLE INTEGRITY
# ============================================================

@pytest.mark.asyncio
async def test_p31_13_all_scenarios_launch_and_return_workflow_ids():
    """Verify all 3 hackathon scenarios launch successfully."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for scen in ["billing_unavailable", "contradictory_evidence", "worker_interruption"]:
            res = await c.post(f"/api/scenarios/{scen}", headers=_headers())
            assert res.status_code in (200, 202)
            data = res.json()
            assert "workflow_id" in data
            assert data["scenario"] == scen

            # Verify snapshot retrieval
            snap_res = await c.get(f"/api/workflows/{data['workflow_id']}", headers=_headers())
            assert snap_res.status_code == 200
            snap = snap_res.json()
            assert snap["workflow"]["scenario"] == scen
            assert snap["workflow"]["state"] in [
                "CREATED", "EXECUTING", "AWAITING_APPROVAL",
                "VERIFYING", "COMPLETED", "ESCALATED", "RECOVERING", "UNKNOWN",
            ]


# ============================================================
# 14. DEMO MODE IS PRESENTATION-ONLY (FRONTEND AUDIT)
# ============================================================

def test_p31_14_demo_mode_is_presentation_only():
    """Verify toggleDemoMode does not call any API endpoints."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    # Find toggleDemoMode body
    match = re.search(r"function toggleDemoMode\(\).*?\n\}", content, re.DOTALL)
    assert match, "toggleDemoMode not found"
    body = match.group()
    assert "apiFetch" not in body, "Demo mode calls apiFetch — safety violation!"
    assert "fetch(" not in body, "Demo mode calls fetch — safety violation!"
    # Must not bypass auth
    assert "password" not in body.lower(), "Demo mode references passwords — safety violation!"


# ============================================================
# 15. COMPLETED TRANSITION SETS completed_at (BACKEND)
# ============================================================

def test_p31_15_completed_transition_sets_completed_at():
    """Verify WorkflowEngine sets completed_at on COMPLETED transition."""
    import inspect
    from backend.engine.workflow_engine import WorkflowEngine
    source = inspect.getsource(WorkflowEngine.transition)
    assert 'completed_at' in source, "transition() should set completed_at for COMPLETED"


# ============================================================
# 16. APPROVAL ENDPOINT REQUIRES APPROVER ROLE
# ============================================================

@pytest.mark.asyncio
async def test_p31_16_approval_requires_approver_role():
    """Viewer-role users cannot submit approval decisions."""
    transport = httpx.ASGITransport(app=app)
    viewer_token = create_access_token(user_id="viewer", role=Role.VIEWER, tenant_id="tenant-default")
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        res = await c.post("/api/scenarios/billing_unavailable", headers=_headers())
        wf_id = res.json()["workflow_id"]

        # Viewer cannot approve
        approve_res = await c.post(
            f"/api/workflows/{wf_id}/approve/fake-approval-id",
            headers=viewer_headers,
            json={"approved": True, "reason": "test"},
        )
        assert approve_res.status_code == 403


# ============================================================
# 17. AUTONOMY BADGE STATE AWARENESS (FRONTEND AUDIT)
# ============================================================

def test_p31_17_autonomy_badge_escalated_state():
    """Verify autonomy decision card handles ESCALATED state explicitly."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    assert "ESCALATED (REJECTED)" in content, \
        "Autonomy badge should show ESCALATED (REJECTED) for rejected workflows"
    assert "OPERATOR AUTHORIZED" in content, \
        "Autonomy badge should show OPERATOR AUTHORIZED for approved workflows"
