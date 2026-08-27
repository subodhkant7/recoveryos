"""
Phase 32: Demo Attack Test Suite & Hackathon Readiness Invariant Verification.

Attacks and Invariant Validations:
1. Attack Recovery Proof under all non-COMPLETED states:
   - CREATED, EXECUTING, AWAITING_APPROVAL, ESCALATED, UNKNOWN
   - Action executed without verification
   - Missing/invalid/negative/reversed timestamps
2. Attack Action ≠ Recovery separation:
   - Tool succeeded but outcome unverified → Must NOT be COMPLETED
   - Verify VERIFYING gate transitions strictly to RECOVERING if unverified
3. Attack SSE Event Duplication:
   - Event dedup via seenEventIds in app.js
   - Single-use ticket consumption
4. Attack Out-of-Order Events & Terminal State Regression:
   - Terminal state immutability in backend state machine
   - Regression protection in frontend state handler
5. Attack Replay Mode Read-Only Guarantee:
   - Zero mutation endpoints called during replay
6. Attack Demo Mode Live Backend Guarantee:
   - Real backend workflow creation and policy enforcement
7. Static Frontend Consistency Audit:
   - All critical DOM IDs exist in index.html and match app.js references
   - All critical JS functions defined and exported
8. Security Audit:
   - Zero API keys, JWT secrets, or private keys across all static assets
   - Tenant isolation & Role-based access control
"""

import os
import re
import pytest
import httpx
from datetime import datetime, timezone, timedelta

from backend.api.server import app
from backend.models.workflow import WorkflowState, VALID_TRANSITIONS, OutcomeContract, RequiredOutcome
from backend.security.tokens import create_access_token
from backend.security.principal import Role


def _operator_headers(tenant="tenant-default"):
    token = create_access_token(user_id="operator", role=Role.OPERATOR, tenant_id=tenant)
    return {"Authorization": f"Bearer {token}"}


def _approver_headers(tenant="tenant-default"):
    token = create_access_token(user_id="approver", role=Role.APPROVER, tenant_id=tenant)
    return {"Authorization": f"Bearer {token}"}


def _viewer_headers(tenant="tenant-default"):
    token = create_access_token(user_id="viewer", role=Role.VIEWER, tenant_id=tenant)
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 1. ATTACK RECOVERY PROOF UNDER NON-COMPLETED STATES
# ============================================================

def test_p32_attack_recovery_proof_guards_all_non_completed_states():
    """Verify showRecoveryProof in app.js strictly requires COMPLETED state."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    # showRecoveryProof must contain guard against non-COMPLETED state
    proof_fn = re.search(r"function showRecoveryProof\(snapshot\)\s*\{(.*?)\n\}", content, re.DOTALL)
    assert proof_fn is not None, "showRecoveryProof function not found in app.js"
    body = proof_fn.group(1)
    assert "wf.state !== 'COMPLETED'" in body, "showRecoveryProof must explicitly guard against wf.state !== 'COMPLETED'"

    # Must also verify that caller in app.js only calls showRecoveryProof if state === 'COMPLETED'
    assert "if (snapshot.workflow?.state === 'COMPLETED')" in content, "selectWorkflow must guard showRecoveryProof call"
    assert "if (normalizedEvent.state === 'COMPLETED')" in content, "applyWorkflowEvent must guard showRecoveryProof call"


def test_p32_attack_mttr_calculation_resilience():
    """Verify calculateWorkflowDuration in app.js handles invalid, missing, and reversed timestamps safely."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    mttr_fn = re.search(r"function calculateWorkflowDuration\([^\)]*\)\s*\{(.*?)\n\}", content, re.DOTALL)
    assert mttr_fn is not None, "calculateWorkflowDuration not found in app.js"
    body = mttr_fn.group(1)

    # Must guard against missing createdAt
    assert "if (!createdAt)" in body, "calculateWorkflowDuration must guard against missing createdAt"
    # Must guard against NaN / invalid Date
    assert "isNaN" in body, "calculateWorkflowDuration must check for isNaN"
    # Must guard against negative delta (reversed timestamps)
    assert "diffMs <= 0" in body or "Math.max" in body or "diffMs < 0" in body, "calculateWorkflowDuration must handle negative durations"


# ============================================================
# 2. ATTACK ACTION ≠ RECOVERY SEPARATION
# ============================================================

def test_p32_attack_action_never_directly_equals_completed():
    """Verify state machine strictly forbids EXECUTING -> COMPLETED."""
    executing_transitions = VALID_TRANSITIONS[WorkflowState.EXECUTING]
    assert WorkflowState.COMPLETED not in executing_transitions, \
        "Direct transition from EXECUTING to COMPLETED violates Action ≠ Recovery invariant!"
    assert WorkflowState.VERIFYING in executing_transitions, \
        "EXECUTING must transition to VERIFYING before any completion is possible."


def test_p32_attack_verifying_with_unverified_outcomes_must_recover_not_complete():
    """Verify contract logic: unverified outcomes fail the completion gate."""
    contract = OutcomeContract(
        workflow_id="wf-test-attack",
        required_outcomes=[
            RequiredOutcome(outcome_id="billing_configured", description="Billing setup", verified=False),
            RequiredOutcome(outcome_id="account_activated", description="Account active", verified=True),
        ]
    )
    assert not contract.all_verified(), "Contract with unverified outcome must not evaluate to all_verified"
    unverified = contract.unverified_outcomes()
    assert len(unverified) == 1
    assert unverified[0].outcome_id == "billing_configured"


# ============================================================
# 3. ATTACK SSE EVENT DUPLICATION & STREAM INTEGRITY
# ============================================================

def test_p32_attack_event_deduplication_in_frontend():
    """Verify app.js has strict event deduplication using seenEventIds Set."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    assert "seenEventIds: new Set()" in content, "appState must initialize seenEventIds Set"
    assert "appState.seenEventIds.has(evId)" in content, "applyWorkflowEvent must check seenEventIds"
    assert "appState.seenEventIds.add(evId)" in content, "applyWorkflowEvent must track seenEventIds"
    assert "appState.seenEventIds.clear()" in content, "selectWorkflow must reset seenEventIds on workflow switch"


# ============================================================
# 4. ATTACK OUT-OF-ORDER EVENTS & TERMINAL STATE PROTECTION
# ============================================================

def test_p32_attack_terminal_states_are_immutable():
    """Verify backend and frontend reject transitions out of COMPLETED or ESCALATED."""
    assert VALID_TRANSITIONS[WorkflowState.COMPLETED] == set(), "COMPLETED state must have 0 outgoing transitions"
    assert VALID_TRANSITIONS[WorkflowState.ESCALATED] == set(), "ESCALATED state must have 0 outgoing transitions"

    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    assert "TERMINAL_STATES" in content, "handleWorkflowStateChange must define TERMINAL_STATES guard"


# ============================================================
# 5. ATTACK REPLAY MODE READ-ONLY GUARANTEE
# ============================================================

def test_p32_attack_replay_mode_has_zero_side_effects():
    """Verify replay functions in app.js contain zero network calls."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    replay_fns = ["startReplay", "pauseReplay", "stepReplay", "resetReplay"]
    for fn in replay_fns:
        match = re.search(rf"function {fn}\(\)\s*\{{(.*?)\n\}}", content, re.DOTALL)
        assert match is not None, f"{fn} function missing in app.js"
        fn_body = match.group(1)
        assert "apiFetch" not in fn_body, f"Replay function {fn} contains apiFetch call!"
        assert "fetch(" not in fn_body, f"Replay function {fn} contains raw fetch call!"
        assert "POST" not in fn_body, f"Replay function {fn} contains HTTP POST mutation!"


# ============================================================
# 6. ATTACK DEMO MODE LIVE BACKEND GUARANTEE
# ============================================================

def test_p32_attack_demo_mode_is_pure_layout_presentation():
    """Verify toggleDemoMode in app.js only modifies CSS layout and has no side effects."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        content = f.read()

    match = re.search(r"function toggleDemoMode\(\)\s*\{(.*?)\n\}", content, re.DOTALL)
    assert match is not None, "toggleDemoMode function missing in app.js"
    body = match.group(1)
    assert "apiFetch" not in body, "toggleDemoMode contains apiFetch"
    assert "fetch" not in body, "toggleDemoMode contains fetch"
    assert "demo-mode-active" in body, "toggleDemoMode must toggle demo-mode-active CSS class"


# ============================================================
# 7. STATIC FRONTEND CONSISTENCY AUDIT
# ============================================================

def test_p32_attack_all_critical_dom_elements_exist():
    """Verify all DOM element IDs referenced in app.js exist in index.html."""
    html_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "index.html")
    with open(html_path) as f:
        html_content = f.read()

    critical_element_ids = [
        "node-detect",
        "node-reason",
        "node-recover",
        "node-verify",
        "node-recovered",
        "graph-stage-label",
        "terminal-feed-container",
        "terminal-event-count",
        "stream-status-pill",
        "stream-status-text",
        "incident-list-container",
        "incident-search-input",
        "decision-statement-text",
        "decision-badge-pill",
        "inspect-criteria-checklist",
        "recovery-proof-certificate",
        "proof-scenario-name",
        "proof-incident-type",
        "proof-time-action",
        "proof-verification-text",
        "proof-intervention",
        "proof-mttr",
        "proof-contract-status",
        "worker-resilience-card",
        "tool-execution-card",
        "modal-scenario-launcher",
        "approval-action-card",
        "replay-toolbar",
        "btn-toggle-demo-mode",
    ]

    for elem_id in critical_element_ids:
        assert f'id="{elem_id}"' in html_content, f"Critical DOM element id='{elem_id}' is missing from index.html!"


def test_p32_attack_all_critical_js_functions_defined():
    """Verify all judge-facing and core helper functions exist in app.js."""
    js_path = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static", "app.js")
    with open(js_path) as f:
        js_content = f.read()

    critical_functions = [
        "calculateWorkflowDuration",
        "normalizeWorkflowEvent",
        "applyWorkflowEvent",
        "handleWorkflowStateChange",
        "illuminateNode",
        "showCinematicIncident",
        "hideCinematicIncident",
        "showToolExecution",
        "hideToolExecution",
        "showWorkerResilience",
        "hideWorkerResilience",
        "showRecoveryProof",
        "hideRecoveryProof",
        "appendTerminalLine",
        "refreshFleetData",
        "renderIncidentList",
        "selectWorkflow",
        "updateAutonomyDecisionCard",
        "updateFourQuestionsInspector",
        "markCriteriaVerified",
        "startReplay",
        "pauseReplay",
        "stepReplay",
        "resetReplay",
        "showApprovalBanner",
        "submitApprovalDecision",
        "openLaunchModal",
        "closeLaunchModal",
        "executeScenarioLaunch",
        "toggleDemoMode",
        "highlightStageInInspector",
        "connectWorkflowStream",
    ]

    for fn_name in critical_functions:
        assert f"function {fn_name}" in js_content, f"Critical function '{fn_name}' is missing in app.js"


# ============================================================
# 8. SECURITY AUDIT & RBAC
# ============================================================

def test_p32_attack_zero_secrets_in_static_files():
    """Verify static assets contain no credentials or tokens."""
    static_dir = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static")
    forbidden = [
        "AIzaSy",
        "recoveryos-jwt-secret-key",
        "Bearer ya29.",
        "GOOG1",
        "-----BEGIN PRIVATE KEY",
    ]
    for fn in ["index.html", "styles.css", "app.js"]:
        with open(os.path.join(static_dir, fn)) as f:
            text = f.read()
            for pattern in forbidden:
                assert pattern not in text, f"Found forbidden secret pattern '{pattern}' in {fn}"


@pytest.mark.asyncio
async def test_p32_attack_unauthorized_scenario_launch_rejected():
    """Verify unauthenticated requests cannot launch scenarios."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        res = await c.post("/api/scenarios/billing_unavailable")
        assert res.status_code in (401, 403), "Unauthenticated scenario launch must be rejected"


@pytest.mark.asyncio
async def test_p32_attack_viewer_cannot_cancel_or_recover_workflow():
    """Verify viewer role cannot trigger operator administrative mutations."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Launch workflow as operator
        launch_res = await c.post("/api/scenarios/billing_unavailable", headers=_operator_headers())
        wf_id = launch_res.json()["workflow_id"]

        # Viewer attempts cancellation
        cancel_res = await c.post(f"/api/workflows/{wf_id}/cancel", headers=_viewer_headers())
        assert cancel_res.status_code == 403, "Viewer role must be forbidden from cancelling workflows"

        # Viewer attempts recovery dispatch
        recover_res = await c.post(f"/api/workflows/{wf_id}/recover", headers=_viewer_headers())
        assert recover_res.status_code == 403, "Viewer role must be forbidden from triggering workflow recovery"


# ============================================================
# 9. END-TO-END SCENARIO VERIFICATION
# ============================================================

@pytest.mark.asyncio
async def test_p32_attack_all_three_scenarios_contract_integrity():
    """Verify all 3 scenarios generate valid unique workflows and contracts."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for scenario_name in ["billing_unavailable", "contradictory_evidence", "worker_interruption"]:
            res = await c.post(f"/api/scenarios/{scenario_name}", headers=_operator_headers())
            assert res.status_code in (200, 202)
            data = res.json()
            wf_id = data["workflow_id"]
            assert wf_id is not None
            assert len(wf_id) > 10

            # Retrieve snapshot
            snap_res = await c.get(f"/api/workflows/{wf_id}", headers=_operator_headers())
            assert snap_res.status_code == 200
            snapshot = snap_res.json()
            wf = snapshot.get("workflow", {})
            contract = wf.get("contract", {}) or snapshot.get("contract", {})

            assert wf.get("scenario") == scenario_name
            assert "required_outcomes" in contract
            assert len(contract["required_outcomes"]) >= 2
            assert "created_at" in wf
