"""
Phase 5.4.1 Durable Persistence & Crash-Safe State Test Suite.

Verifies:
1. create workflow -> restart store -> workflow survives
2. recovery plan -> restart -> plan survives
3. idempotency record -> restart -> record survives
4. human approval -> restart -> approval survives
5. event/audit history -> restart -> history survives
6. interrupted operation -> workflow enters safe recoverable state
7. stale state update cannot overwrite newer state (OCC StaleWorkflowStateError)
8. concurrent state updates are handled safely
9. terminal workflow cannot be mutated after restart
10. UNKNOWN state machine and reconciliation lifecycle
"""

import asyncio
import copy
import uuid
import pytest
from datetime import datetime, timezone

from backend.engine.workflow_engine import WorkflowEngine, InvalidTransitionError
from backend.models.events import EventType
from backend.models.policy import PolicyOutcome
from backend.models.recovery import RecoveryPlanStatus
from backend.models.workflow import WorkflowState
from backend.persistence.workflow_store import (
    InMemoryWorkflowStore,
    StaleWorkflowStateError,
    create_workflow_store,
)
from backend.simulation.external_services import SimulatedServices
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
from backend.tools.onboarding.tools import OnboardingTools


@pytest.fixture
def persistence_setup():
    injector = FailureInjector()
    services = SimulatedServices(injector)
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    tools = OnboardingTools(services, store, engine)
    return injector, services, store, engine, tools


@pytest.mark.asyncio
async def test_1_create_workflow_survives_store_restart(persistence_setup):
    """1. Create workflow -> restart store (simulate process restart from snapshot) -> workflow survives."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Survival Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # Export state snapshot (representing disk/database snapshot)
    snapshot = store.export_state()

    # Create new fresh store instance from snapshot (simulating process restart)
    restarted_store = InMemoryWorkflowStore(shared_data=snapshot)
    restarted_engine = WorkflowEngine(restarted_store)

    wf = await restarted_store.get_workflow(workflow_id)
    assert wf is not None
    assert wf["workflow_id"] == workflow_id
    assert wf["state"] == WorkflowState.EXECUTING.value
    assert wf["name"] == "Survival Test"
    assert wf["version"] >= 2


@pytest.mark.asyncio
async def test_2_recovery_plan_survives_store_restart(persistence_setup):
    """2. Recovery plan -> restart -> plan survives intact."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan Survival", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    res = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Stripe down, failover to PayPal",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
    )
    assert res["status"] == "success"
    plan_id = res["plan_id"]

    # Restart store
    snapshot = store.export_state()
    restarted_store = InMemoryWorkflowStore(shared_data=snapshot)

    plan = await restarted_store.get_recovery_plan(workflow_id, plan_id)
    assert plan is not None
    assert plan["plan_id"] == plan_id
    assert plan["target_outcome_id"] == "billing_configured"
    assert plan["status"] == RecoveryPlanStatus.PROPOSED.value

    active = await restarted_store.get_active_recovery_plan(workflow_id)
    assert active is not None
    assert active["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_3_idempotency_record_survives_store_restart(persistence_setup):
    """3. Idempotency record -> restart -> record survives and deduplicates repeat mutation."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Idempotency Survival", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    # Initial mutation
    res1 = await tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")
    assert res1["status"] == "success"
    sub_id = res1["subscription_id"]

    # Restart store
    snapshot = store.export_state()
    restarted_store = InMemoryWorkflowStore(shared_data=snapshot)
    restarted_engine = WorkflowEngine(restarted_store)
    restarted_tools = OnboardingTools(services, restarted_store, restarted_engine)

    # Repeat call against restarted store
    res2 = await restarted_tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")
    assert res2["status"] == "success"
    assert res2["subscription_id"] == sub_id


@pytest.mark.asyncio
async def test_4_human_approval_survives_store_restart(persistence_setup):
    """4. Human approval -> restart -> approval survives and authorizes action."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Approval Survival", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    approval_data = {
        "approval_id": appr_id,
        "workflow_id": workflow_id,
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "acme-001", "provider": "paypal", "plan_tier": "enterprise"},
        "status": "APPROVED",
        "decided_by": "security_admin",
        "decision_reason": "Approved failover",
    }
    await store.save_approval(workflow_id, approval_data)

    # Restart store
    snapshot = store.export_state()
    restarted_store = InMemoryWorkflowStore(shared_data=snapshot)

    appr = await restarted_store.get_approval(workflow_id, appr_id)
    assert appr is not None
    assert appr["status"] == "APPROVED"
    assert appr["decided_by"] == "security_admin"

    authorized = await restarted_store.get_approved_action(
        workflow_id, "setup_billing", {"customer_id": "acme-001", "provider": "paypal", "plan_tier": "enterprise"}
    )
    assert authorized is not None
    assert authorized["approval_id"] == appr_id


@pytest.mark.asyncio
async def test_5_event_audit_history_survives_store_restart(persistence_setup):
    """5. Event/audit history -> restart -> full chronological history survives."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Audit Survival", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    # Restart store
    snapshot = store.export_state()
    restarted_store = InMemoryWorkflowStore(shared_data=snapshot)

    events = await restarted_store.get_events(workflow_id)
    assert len(events) >= 3
    event_types = [e["event_type"] for e in events]
    assert EventType.STATE_CHANGE.value in event_types
    assert EventType.STEP_STARTED.value in event_types
    assert EventType.STEP_COMPLETED.value in event_types


@pytest.mark.asyncio
async def test_6_interrupted_operation_reconciliation(persistence_setup):
    """6. Interrupted operation -> startup reconciliation inspects external state and recovers."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Interrupted Op", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await engine.transition(workflow_id, WorkflowState.UNKNOWN, detail="Simulated in-flight crash")

    # Manually register an interrupted RUNNING step
    step_id = str(uuid.uuid4())
    idem_key = "idem:test:setup_billing:acme-001:provider=paypal"
    step_data = {
        "step_id": step_id,
        "workflow_id": workflow_id,
        "name": "Interrupted Billing",
        "tool_name": "setup_billing",
        "tool_args": {"customer_id": "acme-001", "provider": "paypal", "plan_tier": "enterprise"},
        "idempotency_key": idem_key,
        "status": "RUNNING",
    }
    await store.save_step(workflow_id, step_data)

    # Perform external mutation directly in services (simulating external completion before crash)
    await services.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise", idempotency_key=idem_key)

    # Restart & Reconcile
    snapshot = store.export_state()
    restarted_store = InMemoryWorkflowStore(shared_data=snapshot)
    restarted_engine = WorkflowEngine(restarted_store)

    reconciled_wf = await restarted_engine.reconcile_interrupted_workflow(workflow_id, services)
    assert reconciled_wf is not None
    assert reconciled_wf["state"] == WorkflowState.EXECUTING.value

    step = await restarted_store.get_step(workflow_id, step_id)
    assert step["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_7_stale_state_update_rejected_by_occ(persistence_setup):
    """7. Stale state update cannot overwrite newer state (OCC StaleWorkflowStateError)."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("OCC Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    # Transition 1: version becomes 2
    wf1 = await engine.transition(workflow_id, WorkflowState.EXECUTING)
    assert wf1["version"] == 2

    # Attempting to save with stale expected_version=1 must fail
    with pytest.raises(StaleWorkflowStateError):
        await store.save_workflow(wf1, expected_version=1)


@pytest.mark.asyncio
async def test_8_concurrent_state_updates_safety(persistence_setup):
    """8. Concurrent state updates are handled safely with OCC."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Concurrent Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    wf = await store.get_workflow(workflow_id)
    current_ver = wf["version"]

    # First update succeeds
    await engine.transition(workflow_id, WorkflowState.RECOVERING, expected_version=current_ver)

    # Second concurrent update using same previous version fails with OCC conflict
    with pytest.raises(StaleWorkflowStateError):
        await engine.transition(workflow_id, WorkflowState.AWAITING_APPROVAL, expected_version=current_ver)


@pytest.mark.asyncio
async def test_9_terminal_workflow_immutable_after_restart(persistence_setup):
    """9. Terminal workflow (COMPLETED/ESCALATED) cannot be mutated after restart."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Terminal Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await engine.transition(workflow_id, WorkflowState.ESCALATED, detail="Terminal failure")

    # Restart store
    snapshot = store.export_state()
    restarted_store = InMemoryWorkflowStore(shared_data=snapshot)
    restarted_engine = WorkflowEngine(restarted_store)
    restarted_tools = OnboardingTools(services, restarted_store, restarted_engine)

    # Attempting recovery plan submission must be rejected
    res = await restarted_tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Attempting post-restart mutation on dead workflow",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
    )
    assert res["status"] == "error"
    assert res["error_type"] == "TERMINAL_STATE_ERROR"

    # Attempting invalid state transition must raise InvalidTransitionError
    with pytest.raises(InvalidTransitionError):
        await restarted_engine.transition(workflow_id, WorkflowState.EXECUTING)


@pytest.mark.asyncio
async def test_10_unknown_state_lifecycle(persistence_setup):
    """10. UNKNOWN state machine transitions and safety."""
    injector, services, store, engine, tools = persistence_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Unknown State Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # Transition to UNKNOWN on crash detection
    wf_unk = await engine.transition(workflow_id, WorkflowState.UNKNOWN, detail="Process interrupted")
    assert wf_unk["state"] == WorkflowState.UNKNOWN.value

    # Resume from UNKNOWN to EXECUTING after reconciliation
    wf_res = await engine.transition(workflow_id, WorkflowState.EXECUTING, detail="Resumed post-reconciliation")
    assert wf_res["state"] == WorkflowState.EXECUTING.value
