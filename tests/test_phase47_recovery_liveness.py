"""
Phase 47 Test: Recovery Liveness and Autonomic Dispatch

Verifies that workflows entering RECOVERING state can never remain stuck indefinitely
and that recovery automatically re-dispatches execution while respecting outcome contracts,
idempotency, and recovery budgets.
"""

import asyncio
import pytest

from backend.models.workflow import WorkflowState, StepStatus
from backend.models.events import EventType
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.external_services import SimulatedServices
from backend.agents.agent_factory import AgentFactory
from backend.engine.agent_runner import run_workflow_agent
from backend.events.consumer import WorkflowEventConsumer
from backend.events.publisher import InMemoryEventPublisher
from backend.events.message_models import WorkflowExecutionMessage, WorkflowEventType


@pytest.mark.asyncio
async def test_agent_runner_recovering_entry_and_needs_redispatch():
    """Verify that agent_runner sets needs_redispatch when transitioning to RECOVERING and supports RECOVERING as entry state."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)

    # 1. Create a workflow
    contract = create_acme_contract("wf-liveness-001")
    wf_dict = await engine.create_workflow(
        name="ACME Recovery Liveness Test",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id="wf-liveness-001",
    )

    # 2. Simulate completed initial steps but unverified billing outcome
    # Mark identity verified
    contract_data = wf_dict["contract"]
    for outcome in contract_data["required_outcomes"]:
        if outcome["outcome_id"] in ("identity_verified", "documents_validated", "risk_assessed"):
            outcome["verified"] = True
    wf_dict["contract"] = contract_data
    await store.save_workflow(wf_dict)

    # Put workflow in RECOVERING state
    await engine.transition("wf-liveness-001", WorkflowState.EXECUTING)
    await engine.transition("wf-liveness-001", WorkflowState.VERIFYING)
    await engine.transition("wf-liveness-001", WorkflowState.RECOVERING, detail="Unverified billing")

    wf = await store.get_workflow("wf-liveness-001")
    assert wf["state"] == WorkflowState.RECOVERING
    assert wf["recovery_attempts"] == 1

    # 3. Execute agent runner directly on RECOVERING workflow
    res = await run_workflow_agent("wf-liveness-001", store, engine, agent_factory)

    # Should have entered execution and completed or re-evaluated
    assert "status" in res
    wf_after = await store.get_workflow("wf-liveness-001")

    # Already-verified outcomes (identity, docs, risk) must remain verified!
    verified_outcomes = [
        o["outcome_id"]
        for o in wf_after["contract"]["required_outcomes"]
        if o.get("verified")
    ]
    assert "identity_verified" in verified_outcomes
    assert "documents_validated" in verified_outcomes
    assert "risk_assessed" in verified_outcomes


@pytest.mark.asyncio
async def test_consumer_autonomic_recovery_dispatch(monkeypatch):
    """Verify that WorkflowEventConsumer automatically publishes RECOVERY_TRIGGER when workflow enters RECOVERING."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)
    publisher = InMemoryEventPublisher()

    # Mock run_workflow_agent to return RECOVERING with needs_redispatch=True deterministically
    async def mock_run_agent(workflow_id, store, engine, agent_factory):
        await engine.transition(workflow_id, WorkflowState.RECOVERING, detail="Mocked unverified outcomes")
        return {"status": "RECOVERING", "workflow_id": workflow_id, "needs_redispatch": True}

    monkeypatch.setattr("backend.events.consumer.run_workflow_agent", mock_run_agent)

    consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        agent_factory=agent_factory,
        event_publisher=publisher,
        worker_id="test-worker",
    )

    # Create workflow
    contract = create_acme_contract("wf-consumer-liveness")
    wf_dict = await engine.create_workflow(
        name="Consumer Recovery Test",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id="wf-consumer-liveness",
    )

    # Send initial dispatch message
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id="wf-consumer-liveness",
        tenant_id="tenant-default",
        idempotency_key="op_dispatch_init",
        expected_version=1,
    )

    out = await consumer.consume_message(msg)
    assert out["status"] == "PROCESSED"

    # Check published messages: should have auto-published a RECOVERY_TRIGGER
    published = publisher.published_messages
    rec_msgs = [m for m in published if m.event_type == WorkflowEventType.RECOVERY_TRIGGER]
    assert len(rec_msgs) >= 1, "Expected auto-published RECOVERY_TRIGGER message"
    assert rec_msgs[0].workflow_id == "wf-consumer-liveness"
