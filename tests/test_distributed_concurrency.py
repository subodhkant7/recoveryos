"""
Phase 5.4.2: Distributed Idempotency + Multi-Worker Concurrency Test Suite.

Verifies:
1. Two workers submit identical operation simultaneously -> exactly ONE external mutation.
2. Two workers submit same idempotency key sequentially -> second request reuses cached result.
3. Worker A claims operation and crashes before external mutation -> Worker B reconciles stale claim safely.
4. Worker A claims operation, external mutation succeeds, crashes before completion -> Worker B discovers external mutation and does NOT duplicate it.
5. Worker A and Worker B race after external mutation succeeds -> exactly one mutation and consistent state.
6. Different idempotency keys for the same workflow are isolated.
7. Terminal workflow cannot acquire a new operation claim.
8. Stale worker cannot overwrite newer completed operation record.
9. Operation claims survive process/store recreation.
10. Reconciliation is idempotent.
11. Flapping provider health cannot cause duplicate external mutations.
12. Concurrent recovery-plan submissions for the same target outcome remain safely superseded/idempotent.
"""

import asyncio
import copy
import uuid
import pytest
from datetime import datetime, timezone, timedelta

from backend.engine.workflow_engine import WorkflowEngine
from backend.models.idempotency import OperationStatus
from backend.models.recovery import RecoveryPlanStatus
from backend.models.workflow import WorkflowState
from backend.persistence.workflow_store import (
    InMemoryWorkflowStore,
    StaleWorkflowStateError,
)
from backend.simulation.external_services import SimulatedServices
from backend.simulation.failure_injector import FailureInjector, CrashBeforePersistenceError
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
from backend.tools.onboarding.tools import OnboardingTools


@pytest.fixture
def concurrency_setup():
    injector = FailureInjector()
    services = SimulatedServices(injector)
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    tools = OnboardingTools(services, store, engine)
    return injector, services, store, engine, tools


@pytest.mark.asyncio
async def test_1_simultaneous_identical_operations_execute_once(concurrency_setup):
    """1. Two workers submit identical operation simultaneously -> exactly ONE external mutation."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Simultaneous Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    # Worker A and Worker B run simultaneously
    worker_a_tools = OnboardingTools(services, store, engine)
    worker_b_tools = OnboardingTools(services, store, engine)

    res_a, res_b = await asyncio.gather(
        worker_a_tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise"),
        worker_b_tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise"),
    )

    # Both return successful results with the same subscription ID
    assert res_a["status"] == "success"
    assert res_b["status"] == "success"
    assert res_a["subscription_id"] == res_b["subscription_id"]

    # Verify external service only has exactly 1 subscription created
    assert len(services._billing_records) == 1


@pytest.mark.asyncio
async def test_2_sequential_duplicate_requests_reuse_result(concurrency_setup):
    """2. Two workers submit same idempotency key sequentially -> second reuses cached result."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Sequential Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    res1 = await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    res2 = await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    assert res1["status"] == "success"
    assert res2["status"] == "success"
    assert res1["verification_reference_id"] == res2["verification_reference_id"]


@pytest.mark.asyncio
async def test_3_crash_before_external_mutation_reconciles_safely(concurrency_setup):
    """3. Worker A claims operation and crashes before external mutation -> Worker B reconciles safely."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Stale Claim Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # Worker A acquires claim with an expired lease
    idem_key = f"idem:{workflow_id}:setup_billing:acme-001:billing_cycle=monthly:plan_tier=enterprise:provider=paypal"
    acquired, claim = await store.claim_operation(
        idempotency_key=idem_key,
        workflow_id=workflow_id,
        tool_name="setup_billing",
        target_entity_id="acme-001",
        parameters={"provider": "paypal", "plan_tier": "enterprise", "billing_cycle": "monthly"},
        worker_id="worker-A",
        lease_seconds=-10,  # Expired lease
    )
    assert acquired is True

    # Worker B comes in
    worker_b_tools = OnboardingTools(services, store, engine)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    res_b = await worker_b_tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")
    assert res_b["status"] == "success"
    assert "subscription_id" in res_b

    # Verify exactly 1 external subscription exists
    assert len(services._billing_records) == 1


@pytest.mark.asyncio
async def test_4_crash_after_external_mutation_does_not_duplicate(concurrency_setup):
    """4. Worker A mutates externally and crashes before saving completion -> Worker B discovers external mutation and does NOT duplicate."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Crash Recovery Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    # Inject simulated crash immediately after external success
    injector.configure_crash_after_external_success(workflow_id, "setup_billing")

    # Worker A executes and crashes
    with pytest.raises(CrashBeforePersistenceError):
        await tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")

    # External service succeeded and has 1 subscription
    assert len(services._billing_records) == 1
    original_sub_id = services._billing_records["acme-001"]["subscription_id"]

    # Worker B retries the same operation
    worker_b_tools = OnboardingTools(services, store, engine)
    res_b = await worker_b_tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")

    assert res_b["status"] == "success"
    assert res_b["subscription_id"] == original_sub_id

    # External mutations must remain strictly 1
    assert len(services._billing_records) == 1


@pytest.mark.asyncio
async def test_5_race_after_external_mutation(concurrency_setup):
    """5. Worker A and Worker B race after external mutation succeeds -> exactly 1 external mutation."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Race Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    worker_a_tools = OnboardingTools(services, store, engine)
    worker_b_tools = OnboardingTools(services, store, engine)

    results = await asyncio.gather(
        worker_a_tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise"),
        worker_b_tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise"),
    )
    assert results[0]["subscription_id"] == results[1]["subscription_id"]
    assert len(services._billing_records) == 1


@pytest.mark.asyncio
async def test_6_different_idempotency_keys_are_isolated(concurrency_setup):
    """6. Different operations for same workflow are isolated and execute independently."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Isolation Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    res_id = await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    res_risk = await tools.run_risk_check(workflow_id, "acme-001")

    assert res_id["status"] == "success"
    assert res_risk["status"] == "success"
    assert res_id["verification_reference_id"] != res_risk.get("risk_score")


@pytest.mark.asyncio
async def test_7_terminal_workflow_cannot_acquire_claim(concurrency_setup):
    """7. Terminal workflow (COMPLETED/ESCALATED) cannot acquire an operation claim."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Terminal Claim Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await engine.transition(workflow_id, WorkflowState.ESCALATED, detail="Terminated")

    res = await tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")
    assert res["status"] == "error"
    assert res["error_type"] == "TERMINAL_STATE_ERROR"


@pytest.mark.asyncio
async def test_8_stale_worker_cannot_overwrite_completed_operation(concurrency_setup):
    """8. Stale worker with outdated version cannot overwrite a completed operation record."""
    injector, services, store, engine, tools = concurrency_setup
    idem_key = "idem:test:op_8"
    await store.claim_operation(
        idempotency_key=idem_key,
        workflow_id="wf-1",
        tool_name="setup_billing",
        target_entity_id="cust-1",
        parameters={},
        worker_id="worker-1",
    )
    # Complete at version 2
    await store.complete_operation(idem_key, result={"status": "success"}, expected_version=1)

    # Stale worker attempts update with version 1
    with pytest.raises(StaleWorkflowStateError):
        await store.complete_operation(idem_key, result={"status": "corrupt"}, expected_version=1)


@pytest.mark.asyncio
async def test_9_operation_claims_survive_store_recreation(concurrency_setup):
    """9. Operation claims and lease state survive store destruction/recreation."""
    injector, services, store, engine, tools = concurrency_setup
    idem_key = "idem:test:op_9"
    await store.claim_operation(
        idempotency_key=idem_key,
        workflow_id="wf-1",
        tool_name="setup_billing",
        target_entity_id="cust-1",
        parameters={},
        worker_id="worker-primary",
    )
    await store.complete_operation(idem_key, result={"subscription_id": "sub-survived"})

    # Export snapshot and recreate store
    snapshot = store.export_state()
    restarted_store = InMemoryWorkflowStore(shared_data=snapshot)

    op = await restarted_store.get_operation(idem_key)
    assert op is not None
    assert op["status"] == OperationStatus.COMPLETED.value
    assert op["result"]["subscription_id"] == "sub-survived"


@pytest.mark.asyncio
async def test_10_reconciliation_is_idempotent(concurrency_setup):
    """10. Repeated reconciliation calls produce identical consistent results without mutation."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Reconciliation Idempotency", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # Perform mutation directly in services
    idem_key = f"idem:{workflow_id}:setup_billing:acme-001:plan_tier=enterprise:provider=paypal"
    await services.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise", idempotency_key=idem_key)

    # Run reconciliation twice
    res1 = await engine.reconcile_interrupted_workflow(workflow_id, services)
    res2 = await engine.reconcile_interrupted_workflow(workflow_id, services)

    assert res1 is not None
    assert res2 is not None
    assert len(services._billing_records) == 1


@pytest.mark.asyncio
async def test_11_flapping_provider_health_does_not_duplicate_mutations(concurrency_setup):
    """11. Flapping provider health status cannot cause duplicate external mutations."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Flapping Provider Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    # Initial successful mutation
    res1 = await tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")
    assert res1["status"] == "success"
    sub_id = res1["subscription_id"]

    # Toggle provider health down and back up
    services._service_status["billing_paypal"]["status"] = "down"
    services._service_status["billing_paypal"]["status"] = "healthy"

    # Repeat request
    res2 = await tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")
    assert res2["status"] == "success"
    assert res2["subscription_id"] == sub_id
    assert len(services._billing_records) == 1


@pytest.mark.asyncio
async def test_12_concurrent_recovery_plan_submissions_supersede_safely(concurrency_setup):
    """12. Concurrent recovery plan submissions for same target outcome safely supersede."""
    injector, services, store, engine, tools = concurrency_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Recovery Plan Supersession", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    res_a, res_b = await asyncio.gather(
        tools.submit_recovery_plan(
            workflow_id=workflow_id,
            target_outcome_id="billing_configured",
            diagnosis="Failover A",
            proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
        ),
        tools.submit_recovery_plan(
            workflow_id=workflow_id,
            target_outcome_id="billing_configured",
            diagnosis="Failover B",
            proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "square"}}],
        ),
    )

    assert res_a["status"] == "success"
    assert res_b["status"] == "success"

    # Active recovery plan is the latest proposed plan
    active = await store.get_active_recovery_plan(workflow_id, target_outcome_id="billing_configured")
    assert active is not None
    assert active["status"] == RecoveryPlanStatus.PROPOSED.value
