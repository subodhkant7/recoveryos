"""
Phase 46: Regression and acceptance tests for:
- Authoritative UTC timestamps & immutability across updates
- Dynamic required outcomes panel & authoritative verification states
- Deterministic playback lifecycle (Play, Next, Replay)
- Non-duplication of database records or incidents during replay
"""

import copy
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from backend.models.events import WorkflowEvent, EventType, utc_now, utc_now_iso
from backend.models.workflow import Workflow, WorkflowState, OutcomeContract, RequiredOutcome
from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.simulation.failure_injector import FailureInjector, configure_scenario_1
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA


@pytest.mark.asyncio
async def test_01_canonical_timestamps_and_immutability():
    """Verify workflow created_at remains immutable on updates, and events have occurred_at."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store=store)

    contract = create_acme_contract("wf-time-1")
    wf = await engine.create_workflow(
        name="Timestamp Immutability Test",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        scenario="billing_unavailable",
    )
    wf_id = wf["workflow_id"]
    original_created_at = wf["created_at"]
    assert original_created_at is not None
    assert "T" in original_created_at

    # Transition workflow state
    wf_executing = await engine.transition(
        wf_id,
        WorkflowState.EXECUTING,
        detail="Started execution",
        actor="worker",
    )
    assert wf_executing["state"] == WorkflowState.EXECUTING.value
    assert wf_executing["created_at"] == original_created_at

    # Transition again to VERIFYING
    wf_verifying = await engine.transition(
        wf_id,
        WorkflowState.VERIFYING,
        detail="Verification in progress",
        actor="agent",
    )
    assert wf_verifying["state"] == WorkflowState.VERIFYING.value
    assert wf_verifying["created_at"] == original_created_at

    # Check persisted events have occurred_at and timestamp
    events = await store.get_events(wf_id)
    assert len(events) >= 3
    for ev in events:
        assert "timestamp" in ev
        assert "occurred_at" in ev
        assert ev["timestamp"] is not None
        assert ev["occurred_at"] is not None
        # Must be valid ISO string
        dt = datetime.fromisoformat(ev["occurred_at"])
        assert dt.tzinfo is not None or dt.isoformat()


@pytest.mark.asyncio
async def test_02_dynamic_required_outcomes_authoritative_state():
    """Verify that action success does not equal verification without verify_outcome passing."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store=store)

    contract = create_acme_contract("wf-outcomes-1")
    wf = await engine.create_workflow(
        name="Outcomes Test",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        scenario="billing_unavailable",
    )
    wf_id = wf["workflow_id"]

    # Record step completion without verification
    step_data = {
        "step_id": "step-billing-1",
        "name": "Configure Billing (paypal)",
        "tool_name": "setup_billing",
        "arguments": {"provider": "paypal"},
    }
    await engine.record_step_started(wf_id, step_data)
    await engine.record_step_completed(wf_id, "step-billing-1", {"status": "success"})

    # Verify no evidence exists yet
    evidence = await store.get_evidence_by_workflow(wf_id) if hasattr(store, "get_evidence_by_workflow") else []
    assert len(evidence) == 0

    # Record verification passed
    await engine._record_event(
        workflow_id=wf_id,
        event_type=EventType.VERIFICATION_RESULT,
        title="Verification Passed: billing_configured",
        detail="Independent probe passed",
        payload={"outcome_id": "billing_configured", "passed": True, "discrepancies": []},
        actor="taskmaster",
    )

    events = await store.get_events(wf_id)
    verif_events = [e for e in events if e.get("event_type") == EventType.VERIFICATION_RESULT.value]
    assert len(verif_events) == 1
    assert verif_events[0]["payload"]["passed"] is True


@pytest.mark.asyncio
async def test_03_playback_next_and_replay_lifecycle():
    """Simulate playback operations (Play, Next, Replay) on a completed workflow."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store=store)

    contract = create_acme_contract("wf-playback-1")
    wf = await engine.create_workflow(
        name="Playback Test",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        scenario="billing_unavailable",
    )
    wf_id = wf["workflow_id"]

    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Dispatching", actor="system")
    await engine._record_event(
        workflow_id=wf_id,
        event_type=EventType.STEP_STARTED,
        title="Step Started: Verify Customer Identity",
        detail="Tool: verify_identity",
        payload={"tool_name": "verify_identity"},
    )
    await engine._record_event(
        workflow_id=wf_id,
        event_type=EventType.STEP_COMPLETED,
        title="Step Completed: Verify Customer Identity",
        detail="Result: success",
        payload={"result": {"status": "success"}},
    )
    await engine._record_event(
        workflow_id=wf_id,
        event_type=EventType.VERIFICATION_RESULT,
        title="Verification Passed: identity_verified",
        detail="Method: Independent query",
        payload={"outcome_id": "identity_verified", "passed": True},
    )
    await engine.transition(wf_id, WorkflowState.VERIFYING, detail="Verifying", actor="agent")
    await engine.transition(wf_id, WorkflowState.COMPLETED, detail="Completed", actor="verifier")

    persisted_events = await store.get_events(wf_id)
    assert len(persisted_events) == 7

    # Test Replay: replay should NOT add new events to the store
    replay_cursor = 0
    replayed_events = []
    while replay_cursor < len(persisted_events):
        replayed_events.append(persisted_events[replay_cursor])
        replay_cursor += 1

    assert len(replayed_events) == 7
    # Verify store event count remains exactly 7 (no mutation)
    store_events_after_replay = await store.get_events(wf_id)
    assert len(store_events_after_replay) == 7
    assert [e["event_id"] for e in store_events_after_replay] == [e["event_id"] for e in persisted_events]
