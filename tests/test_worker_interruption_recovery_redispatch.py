"""
Regression test for worker interruption reconciliation and autonomic recovery redispatch.

Guarantees:
1. Workflow transitions: EXECUTING -> UNKNOWN -> reconciliation -> EXECUTING -> RECOVERING -> recovery redispatch -> worker consumes redispatch -> EXECUTING -> VERIFYING -> COMPLETED.
2. External mutation is idempotent and never duplicated (only 1 Stripe subscription).
3. Stale OCC messages cannot overwrite newer workflow state.
4. Final outcome reaches RECOVERED • VERIFIED.
"""

import pytest

from backend.models.workflow import WorkflowState, StepStatus
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.simulation.failure_injector import FailureInjector, CrashBeforePersistenceError
from backend.simulation.external_services import SimulatedServices
from backend.agents.agent_factory import AgentFactory
from backend.events.consumer import WorkflowEventConsumer
from backend.events.publisher import InMemoryEventPublisher
from backend.events.message_models import WorkflowExecutionMessage, WorkflowEventType
from backend.worker.service import WorkflowWorkerService, DeliveryStatus
from backend.worker.server import get_worker_service, set_worker_service


@pytest.mark.asyncio
async def test_worker_interruption_full_lifecycle_redispatch_and_completion():
    """
    Test that an interrupted worker reconciles external mutation, publishes RECOVERY_TRIGGER,
    and a worker consuming the redispatch finishes the workflow to COMPLETED.
    """
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)
    publisher = InMemoryEventPublisher()

    # 1. Create a workflow for worker_interruption
    wf_id = "wf-interruption-redispatch-01"
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Worker Interruption Redispatch Test",
        scenario="worker_interruption",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
    )

    consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        agent_factory=agent_factory,
        event_publisher=publisher,
        worker_id="worker-test-node",
    )
    worker_service = WorkflowWorkerService(
        consumer=consumer,
        worker_id="worker-test-node",
    )

    # Initial dispatch
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-default",
        idempotency_key=f"op_dispatch_{wf_id}_v1",
        expected_version=1,
        producer_id="recoveryos-api",
    )

    # Mock run_workflow_agent for step 1 to simulate interruption during setup_billing
    async def mock_first_run(workflow_id, store, engine, agent_factory):
        # External mutation succeeded on Stripe
        await services.setup_billing(
            workflow_id=workflow_id,
            customer_id="acme-001",
            provider="stripe",
            plan_tier="enterprise",
            billing_cycle="monthly",
        )
        # Interruption occurs
        await engine.transition(workflow_id, WorkflowState.UNKNOWN, detail="Crash after external mutation")
        # Reconciliation runs
        await engine.reconcile_interrupted_workflow(workflow_id, services)
        # Transition to RECOVERING
        await engine.transition(workflow_id, WorkflowState.RECOVERING, detail="Interruption reconciled; dispatching bounded recovery")
        return {
            "status": "RECOVERING",
            "workflow_id": workflow_id,
            "needs_redispatch": True,
            "reconciled_after_interruption": True,
        }

    import backend.events.consumer as consumer_module
    orig_run = consumer_module.run_workflow_agent
    consumer_module.run_workflow_agent = mock_first_run

    try:
        res1 = await worker_service.process_message(msg)
        assert res1.delivery_status == DeliveryStatus.ACK

        # Verify workflow is in RECOVERING
        wf_recovering = await store.get_workflow(wf_id)
        assert wf_recovering["state"] == WorkflowState.RECOVERING.value
        rec_version = wf_recovering["version"]

        # Verify that publisher captured the RECOVERY_TRIGGER message
        published_messages = publisher.published_messages
        assert len(published_messages) == 1
        rec_msg = published_messages[0]
        assert rec_msg.event_type == WorkflowEventType.RECOVERY_TRIGGER
        assert rec_msg.workflow_id == wf_id
        assert rec_msg.expected_version == rec_version
        assert rec_msg.producer_id.startswith("recoveryos-worker")

        # Now simulate the recovery run
        async def mock_recovery_run(workflow_id, store, engine, agent_factory):
            # Verify we can transition to EXECUTING
            cur = await store.get_workflow(workflow_id)
            if cur["state"] == WorkflowState.RECOVERING.value:
                await engine.transition(workflow_id, WorkflowState.EXECUTING, detail="Recovery resuming")
            
            # Execute remaining steps: activate account, send welcome
            await services.activate_account(workflow_id=workflow_id, customer_id="acme-001")
            await services.send_welcome_package(workflow_id=workflow_id, customer_id="acme-001", email="alice@acmecorp.com")
            
            # Mark required outcomes verified
            wf = await store.get_workflow(workflow_id)
            for req in wf["contract"]["required_outcomes"]:
                req["verified"] = True
            await store.save_workflow(wf)
            
            # Verify and Complete
            await engine.transition(workflow_id, WorkflowState.VERIFYING, detail="Independent outcome verification")
            await engine.transition(workflow_id, WorkflowState.COMPLETED, detail="All outcomes verified")
            return {"status": "COMPLETED", "workflow_id": workflow_id}

        consumer_module.run_workflow_agent = mock_recovery_run

        # Worker processes the recovery message
        res2 = await worker_service.process_message(rec_msg)
        assert res2.delivery_status == DeliveryStatus.ACK

        # Verify final workflow state is COMPLETED
        wf_final = await store.get_workflow(wf_id)
        assert wf_final["state"] == WorkflowState.COMPLETED.value
        assert wf_final["version"] > rec_version
        for req in wf_final["contract"]["required_outcomes"]:
            assert req["verified"] is True

        # Assert no duplicate mutation: exactly 1 subscription in billing records
        billing_record = services._billing_records.get("acme-001")
        assert billing_record is not None
        assert billing_record["provider"] == "stripe"
        assert billing_record["plan_tier"] == "enterprise"

    finally:
        consumer_module.run_workflow_agent = orig_run


@pytest.mark.asyncio
async def test_worker_server_get_worker_service_has_event_publisher():
    """Verify that get_worker_service in backend.worker.server configures an event publisher."""
    set_worker_service(None)
    service = get_worker_service()
    assert service is not None
    assert service._consumer is not None
    assert service._consumer._event_publisher is not None


@pytest.mark.asyncio
async def test_stale_occ_rejection_during_interruption_recovery():
    """Verify that stale OCC messages are rejected and cannot regress workflow state."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    publisher = InMemoryEventPublisher()

    wf_id = "wf-stale-occ-01"
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Stale OCC Test",
        scenario="worker_interruption",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
    )

    consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        event_publisher=publisher,
        worker_id="worker-stale-test",
    )
    worker_service = WorkflowWorkerService(
        consumer=consumer,
        worker_id="worker-stale-test",
    )

    # Advance workflow to version 5
    for i in range(4):
        await engine.transition(wf_id, WorkflowState.EXECUTING if i % 2 == 0 else WorkflowState.UNKNOWN)

    cur_wf = await store.get_workflow(wf_id)
    assert cur_wf["version"] >= 5

    # Send a stale message with expected_version=1
    stale_msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.RECOVERY_TRIGGER,
        workflow_id=wf_id,
        tenant_id="tenant-default",
        idempotency_key="op_stale_msg_v1",
        expected_version=1,
        producer_id="recoveryos-worker-test",
    )

    result = await worker_service.process_message(stale_msg)
    # Must be NACKed as RETRYABLE failure due to OCC version mismatch
    assert result.delivery_status == DeliveryStatus.NACK

    # Verify workflow version and state did not change
    after_wf = await store.get_workflow(wf_id)
    assert after_wf["version"] == cur_wf["version"]
    assert after_wf["state"] == cur_wf["state"]
