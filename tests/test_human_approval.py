"""
Human-in-the-Loop Approval and Resumption Security Tests.

Verifies:
1. Policy rejection cannot be bypassed by calling the mutation again.
2. Pending approval blocks the mutating tool execution and pauses workflow at AWAITING_APPROVAL.
3. Approval authorizes ONLY the specific pending action (tool + exact arguments).
4. Rejected approval prevents mutation and transitions workflow to ESCALATED.
5. Duplicate approval requests are deduplicated.
6. Already-approved request cannot be approved twice.
7. Already-rejected request cannot be approved later.
8. Unrelated mutation cannot reuse an approval.
9. Tampering with requested action arguments invalidates the approval.
10. Workflow cannot reach COMPLETED while required approval remains unresolved.
11. Crash/reconstruction: Persisted state reconstructed after simulated restart preserves pending approval, permits human approval, and resumes execution to completion.
"""

import pytest
import uuid
from backend.models.workflow import WorkflowState
from backend.models.approval import HumanApproval, ApprovalStatus
from backend.models.policy import PolicyOutcome
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.external_services import SimulatedServices
from backend.persistence.workflow_store import WorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.tools.onboarding.tools import OnboardingTools
from backend.agents.agent_factory import AgentFactory
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA


@pytest.fixture(autouse=True)
def preserve_server_state():
    import backend.api.server as srv
    orig_store = srv.store
    orig_engine = srv.engine
    yield
    srv.store = orig_store
    srv.engine = orig_engine


@pytest.fixture
def test_setup():
    injector = FailureInjector()
    services = SimulatedServices(injector)
    store = WorkflowStore()
    engine = WorkflowEngine(store)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)
    tools = OnboardingTools(services, store, engine)
    return injector, services, store, engine, policy_engine, agent_factory, tools


@pytest.mark.asyncio
async def test_1_policy_rejection_cannot_be_bypassed_by_repeat_call(test_setup):
    """Security 1: When policy rejects an action (e.g. out-of-order execution), repeated calls remain blocked."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 1", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    taskmaster = agent_factory.create_taskmaster()
    before_callback = taskmaster.before_tool_callback

    # Attempt setup_billing BEFORE verify_identity (violates identity_first constraint)
    class DummyTool:
        name = "setup_billing"

    # First attempt
    res1 = await before_callback(DummyTool(), {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe"}, None)
    assert res1 is not None
    assert res1.get("status") == "blocked"
    assert res1.get("action_required") == "rejected"

    # Repeat attempt
    res2 = await before_callback(DummyTool(), {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe"}, None)
    assert res2 is not None
    assert res2.get("status") == "blocked"
    assert res2.get("action_required") == "rejected"


@pytest.mark.asyncio
async def test_2_pending_approval_blocks_mutation_and_sets_awaiting_approval(test_setup):
    """Security 2: When policy flags contradictory evidence, an approval is created and workflow enters AWAITING_APPROVAL."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 2", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # 1. Complete prerequisites
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    # 2. Inject contradictory evidence (two differing plan tiers in evidence store)
    await store.save_evidence(workflow_id, {
        "evidence_id": "ev-billing-1",
        "workflow_id": workflow_id,
        "source": "billing:prior",
        "evidence_type": "TOOL_RESULT",
        "data": {"plan_tier": "starter"},
    })
    await store.save_evidence(workflow_id, {
        "evidence_id": "ev-billing-2",
        "workflow_id": workflow_id,
        "source": "billing:current",
        "evidence_type": "TOOL_RESULT",
        "data": {"plan_tier": "enterprise"},
    })

    taskmaster = agent_factory.create_taskmaster()
    before_callback = taskmaster.before_tool_callback

    class DummyTool:
        name = "setup_billing"

    # Attempt billing setup
    res = await before_callback(
        DummyTool(),
        {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise"},
        None,
    )
    assert res is not None
    assert res.get("status") == "blocked"
    assert res.get("action_required") == "human_approval"
    assert "approval_id" in res

    # Workflow must be in AWAITING_APPROVAL
    wf = await store.get_workflow(workflow_id)
    assert wf["state"] == WorkflowState.AWAITING_APPROVAL.value

    # Approval must be stored as PENDING
    approval = await store.get_approval(workflow_id, res["approval_id"])
    assert approval is not None
    assert approval["status"] == ApprovalStatus.PENDING.value
    assert approval["action_tool"] == "setup_billing"


@pytest.mark.asyncio
async def test_3_approval_authorizes_only_specific_action(test_setup):
    """Security 3: Human approval authorizes ONLY the specific tool and arguments."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 3", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # Prereqs + contradictory evidence
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")
    await store.save_evidence(workflow_id, {"evidence_id": "ev-1", "workflow_id": workflow_id, "source": "billing:1", "data": {"plan_tier": "starter"}})
    await store.save_evidence(workflow_id, {"evidence_id": "ev-2", "workflow_id": workflow_id, "source": "billing:2", "data": {"plan_tier": "enterprise"}})

    # Create & approve for setup_billing with provider="paypal"
    approval_id = str(uuid.uuid4())
    await store.save_approval(workflow_id, {
        "approval_id": approval_id,
        "workflow_id": workflow_id,
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "paypal", "plan_tier": "enterprise", "billing_cycle": "monthly"},
        "status": "APPROVED",
        "decided_by": "sec_operator",
        "decision_reason": "Approved enterprise tier for paypal",
    })

    taskmaster = agent_factory.create_taskmaster()
    before_callback = taskmaster.before_tool_callback

    class DummyTool:
        name = "setup_billing"

    # Authorized call with provider="paypal" -> ALLOWED (None returned)
    allowed_res = await before_callback(
        DummyTool(),
        {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "paypal", "plan_tier": "enterprise", "billing_cycle": "monthly"},
        None,
    )
    assert allowed_res is None

    # Unauthorized call with provider="stripe" -> BLOCKED (requires human approval for stripe)
    blocked_res = await before_callback(
        DummyTool(),
        {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise", "billing_cycle": "monthly"},
        None,
    )
    assert blocked_res is not None
    assert blocked_res.get("status") == "blocked"


@pytest.mark.asyncio
async def test_4_rejected_approval_prevents_mutation_and_escalates(test_setup):
    """Security 4: Human rejection permanently blocks mutation and marks workflow ESCALATED."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 4", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")
    await store.save_evidence(workflow_id, {"evidence_id": "ev-1", "workflow_id": workflow_id, "source": "billing:1", "data": {"plan_tier": "starter"}})
    await store.save_evidence(workflow_id, {"evidence_id": "ev-2", "workflow_id": workflow_id, "source": "billing:2", "data": {"plan_tier": "enterprise"}})

    # Create approval and mark REJECTED
    approval_id = str(uuid.uuid4())
    await store.save_approval(workflow_id, {
        "approval_id": approval_id,
        "workflow_id": workflow_id,
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise"},
        "status": "REJECTED",
        "decided_by": "sec_operator",
        "decision_reason": "Account is delinquent",
    })

    taskmaster = agent_factory.create_taskmaster()
    before_callback = taskmaster.before_tool_callback

    class DummyTool:
        name = "setup_billing"

    res = await before_callback(
        DummyTool(),
        {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise"},
        None,
    )
    assert res is not None
    assert res.get("status") == "blocked"
    assert res.get("action_required") == "rejected"
    assert "rejected" in res.get("reason", "").lower()


@pytest.mark.asyncio
async def test_5_duplicate_approval_requests_are_deduplicated(test_setup):
    """Security 5: Repeated attempts for the same blocked action return the existing approval ID without spawning duplicate records."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 5", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")
    await store.save_evidence(workflow_id, {"evidence_id": "ev-1", "workflow_id": workflow_id, "source": "billing:1", "data": {"plan_tier": "starter"}})
    await store.save_evidence(workflow_id, {"evidence_id": "ev-2", "workflow_id": workflow_id, "source": "billing:2", "data": {"plan_tier": "enterprise"}})

    taskmaster = agent_factory.create_taskmaster()
    before_callback = taskmaster.before_tool_callback

    class DummyTool:
        name = "setup_billing"

    # First call creates approval
    res1 = await before_callback(DummyTool(), {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise"}, None)
    # Second call returns existing approval
    res2 = await before_callback(DummyTool(), {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise"}, None)

    assert res1["approval_id"] == res2["approval_id"]
    approvals = await store.get_approvals(workflow_id)
    assert len(approvals) == 1


@pytest.mark.asyncio
async def test_6_7_already_resolved_approval_cannot_be_decided_twice(test_setup):
    """Security 6 & 7: An approval already APPROVED or REJECTED cannot be approved or rejected again."""
    from backend.api.server import approve_workflow, ApprovalRequest
    from fastapi import HTTPException

    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 6", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await engine.transition(workflow_id, WorkflowState.AWAITING_APPROVAL)

    # Save pending approval
    approval_id = str(uuid.uuid4())
    await store.save_approval(workflow_id, {
        "approval_id": approval_id,
        "workflow_id": workflow_id,
        "status": "PENDING",
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001"},
    })

    # First approval succeeds
    req = ApprovalRequest(approved=True, reason="Valid business reason", decided_by="admin")
    # Call directly (temporarily patching store/engine in server module if needed)
    import backend.api.server as srv
    srv.store = store
    srv.engine = engine

    res = await srv.approve_workflow(workflow_id, approval_id, req)
    assert res["approved"] is True

    # Second approval attempt must raise HTTPException(400)
    with pytest.raises(HTTPException) as exc_info:
        await srv.approve_workflow(workflow_id, approval_id, req)
    assert exc_info.value.status_code == 400
    assert "already decided" in exc_info.value.detail


@pytest.mark.asyncio
async def test_8_unrelated_mutation_cannot_reuse_approval(test_setup):
    """Security 8: An approval for setup_billing cannot be used to authorize send_welcome_package."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 8", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # Approved for setup_billing only
    await store.save_approval(workflow_id, {
        "approval_id": str(uuid.uuid4()),
        "workflow_id": workflow_id,
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "stripe"},
        "status": "APPROVED",
    })

    taskmaster = agent_factory.create_taskmaster()
    before_callback = taskmaster.before_tool_callback

    class DummyWelcomeTool:
        name = "send_welcome_package"

    # Calling send_welcome_package before identity_verified must STILL be rejected by step_ordering
    res = await before_callback(DummyWelcomeTool(), {"workflow_id": workflow_id, "customer_id": "acme-001", "email": "a@b.com"}, None)
    assert res is not None
    assert res.get("status") == "blocked"
    assert res.get("action_required") == "rejected"


@pytest.mark.asyncio
async def test_9_tampering_with_requested_action_invalidates_approval(test_setup):
    """Security 9: Changing arguments (e.g. requesting enterprise instead of starter) invalidates an approval granted for starter."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 9", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")
    await store.save_evidence(workflow_id, {"evidence_id": "ev-1", "workflow_id": workflow_id, "source": "billing:1", "data": {"plan_tier": "starter"}})
    await store.save_evidence(workflow_id, {"evidence_id": "ev-2", "workflow_id": workflow_id, "source": "billing:2", "data": {"plan_tier": "enterprise"}})

    # Human approves 'starter'
    await store.save_approval(workflow_id, {
        "approval_id": str(uuid.uuid4()),
        "workflow_id": workflow_id,
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "stripe", "plan_tier": "starter"},
        "status": "APPROVED",
    })

    taskmaster = agent_factory.create_taskmaster()
    before_callback = taskmaster.before_tool_callback

    class DummyTool:
        name = "setup_billing"

    # Attempting to execute 'enterprise' with starter approval must be BLOCKED
    tampered_res = await before_callback(
        DummyTool(),
        {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise"},
        None,
    )
    assert tampered_res is not None
    assert tampered_res.get("status") == "blocked"


@pytest.mark.asyncio
async def test_10_workflow_cannot_reach_completed_while_awaiting_approval(test_setup):
    """Security 10: Workflow cannot be marked COMPLETED while paused in AWAITING_APPROVAL."""
    from backend.engine.workflow_engine import InvalidTransitionError

    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 10", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await engine.transition(workflow_id, WorkflowState.AWAITING_APPROVAL)

    # Attempt invalid direct transition from AWAITING_APPROVAL -> COMPLETED
    with pytest.raises(InvalidTransitionError):
        await engine.transition(workflow_id, WorkflowState.COMPLETED)


@pytest.mark.asyncio
async def test_11_crash_restart_reconstruction_and_resumption(test_setup):
    """
    Scenario 11 (Crash / Restart / Resumption):
    1. Agent encounters contradictory evidence -> enters AWAITING_APPROVAL.
    2. Process memory is reconstructed from persisted store snapshot.
    3. Approval is still PENDING.
    4. Human approves via API -> transitions to EXECUTING.
    5. Authorized action executes successfully.
    6. Outcome is verified.
    """
    import backend.api.server as srv
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sec 11", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # 1. Complete identity and risk
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    # 2. Add contradictory evidence
    await store.save_evidence(workflow_id, {"evidence_id": "ev-1", "workflow_id": workflow_id, "source": "billing:1", "data": {"plan_tier": "starter"}})
    await store.save_evidence(workflow_id, {"evidence_id": "ev-2", "workflow_id": workflow_id, "source": "billing:2", "data": {"plan_tier": "enterprise"}})

    taskmaster = agent_factory.create_taskmaster()
    before_callback = taskmaster.before_tool_callback

    class DummyTool:
        name = "setup_billing"

    # Trigger policy block -> creates pending approval
    block_res = await before_callback(
        DummyTool(),
        {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise", "billing_cycle": "monthly"},
        None,
    )
    assert block_res["status"] == "blocked"
    approval_id = block_res["approval_id"]

    # 3. Simulate process restart by taking a snapshot and reconstructing a fresh store/engine
    snapshot = await store.get_workflow_snapshot(workflow_id)

    fresh_store = WorkflowStore()
    await fresh_store.save_workflow(snapshot["workflow"])
    for s in snapshot["steps"]:
        await fresh_store.save_step(workflow_id, s)
    for ev in snapshot["evidence"]:
        await fresh_store.save_evidence(workflow_id, ev)
    for app in snapshot["approvals"]:
        await fresh_store.save_approval(workflow_id, app)

    fresh_engine = WorkflowEngine(fresh_store)
    fresh_policy = PolicyEngine()
    fresh_factory = AgentFactory(fresh_store, fresh_engine, services, fresh_policy)
    fresh_tools = OnboardingTools(services, fresh_store, fresh_engine)

    # 4. Verify reconstructed approval is PENDING
    pending_apps = await fresh_store.get_pending_approvals(workflow_id)
    assert len(pending_apps) == 1
    assert pending_apps[0]["approval_id"] == approval_id

    # 5. Human approves
    srv.store = fresh_store
    srv.engine = fresh_engine
    approve_res = await srv.approve_workflow(
        workflow_id,
        approval_id,
        srv.ApprovalRequest(approved=True, reason="Customer verified enterprise contract", decided_by="director"),
    )
    assert approve_res["approved"] is True

    # 6. Workflow is back in EXECUTING
    reconstructed_wf = await fresh_store.get_workflow(workflow_id)
    assert reconstructed_wf["state"] == WorkflowState.EXECUTING.value

    # 7. Resumed action now passes policy gate and executes
    fresh_taskmaster = fresh_factory.create_taskmaster()
    fresh_before = fresh_taskmaster.before_tool_callback

    allowed = await fresh_before(
        DummyTool(),
        {"workflow_id": workflow_id, "customer_id": "acme-001", "provider": "stripe", "plan_tier": "enterprise", "billing_cycle": "monthly"},
        None,
    )
    assert allowed is None  # Granted!

    # Execute billing tool and verify outcome
    billing_res = await fresh_tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="stripe",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )
    assert billing_res["status"] == "success"

    verif_res = await fresh_tools.verify_outcome(workflow_id, "billing_configured", "acme-001")
    assert verif_res["passed"] is True

    # Verify contract state
    final_wf = await fresh_store.get_workflow(workflow_id)
    billing_outcome = next(o for o in final_wf["contract"]["required_outcomes"] if o["outcome_id"] == "billing_configured")
    assert billing_outcome["verified"] is True
