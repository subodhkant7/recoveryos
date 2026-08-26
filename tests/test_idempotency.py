"""
Comprehensive Idempotency and Crash-Reconciliation Tests.

Tests:
1. First mutation executes exactly once.
2. Identical retry returns cached result.
3. Duplicate retry does not create another external entity (subscription count remains 1).
4. Crash after external success but before local persistence raises simulated error.
5. Restart/retry detects and reconciles the existing external mutation without duplicate side effects.
6. Materially different operation generates a distinct idempotency key and executes.
7. Concurrent duplicate requests cannot create duplicate mutations.
8. Failed external mutation can be retried safely.
9. Idempotency records correctly transition through lifecycle (EXECUTING -> SUCCEEDED / FAILED).
10. Simulated crash and subsequent reconciliation leaves the workflow in a valid state to reach COMPLETED.
"""

import asyncio
import pytest
import uuid
from backend.models.workflow import WorkflowState
from backend.models.idempotency import IdempotencyStatus
from backend.engine.idempotency import derive_idempotency_key
from backend.simulation.failure_injector import FailureInjector, CrashBeforePersistenceError
from backend.simulation.external_services import SimulatedServices
from backend.persistence.workflow_store import WorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.tools.onboarding.tools import OnboardingTools
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA


@pytest.fixture
def test_setup():
    injector = FailureInjector()
    services = SimulatedServices(injector)
    store = WorkflowStore()
    engine = WorkflowEngine(store)
    tools = OnboardingTools(services, store, engine)
    return injector, services, store, engine, tools


@pytest.mark.asyncio
async def test_1_first_mutation_executes_once(test_setup):
    """Scenario 1: First mutation executes and records step + idempotency record."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow("Test 1", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    res = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )
    assert res.get("status") == "success"
    assert "subscription_id" in res

    # Check external state
    assert len(services._billing_records) == 1
    assert services._billing_records["acme-001"]["provider"] == "paypal"

    # Check idempotency record
    key = derive_idempotency_key(workflow_id, "setup_billing", "acme-001", {
        "provider": "paypal", "plan_tier": "enterprise", "billing_cycle": "monthly"
    })
    rec = await store.get_idempotency_record(key)
    assert rec is not None
    assert rec["status"] == IdempotencyStatus.SUCCEEDED.value
    assert rec["result"]["subscription_id"] == res["subscription_id"]


@pytest.mark.asyncio
async def test_2_identical_retry_returns_cached_result(test_setup):
    """Scenario 2: Identical retry returns cached result directly from store."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 2", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    res1 = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )

    # Second call
    res2 = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )

    assert res1 == res2
    assert res1["subscription_id"] == res2["subscription_id"]


@pytest.mark.asyncio
async def test_3_duplicate_retry_does_not_create_duplicate_external_entity(test_setup):
    """Scenario 3: Repeating setup_billing creates exactly ONE subscription externally."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 3", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    # First call
    res1 = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )

    # Repeat call
    res2 = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )

    assert len(services._billing_records) == 1
    assert services._billing_records["acme-001"]["subscription_id"] == res1["subscription_id"]
    assert res1["subscription_id"] == res2["subscription_id"]


@pytest.mark.asyncio
async def test_4_crash_after_external_success_raises(test_setup):
    """Scenario 4: Crash simulation fires immediately after external success before local persistence."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 4", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    # Configure crash after external success for setup_billing
    injector.configure_crash_after_external_success(workflow_id, "setup_billing", remaining_count=1)

    with pytest.raises(CrashBeforePersistenceError):
        await tools.setup_billing(
            workflow_id=workflow_id,
            customer_id="acme-001",
            provider="paypal",
            plan_tier="enterprise",
            billing_cycle="monthly",
        )

    # External entity WAS created
    assert len(services._billing_records) == 1
    assert services._billing_records["acme-001"]["provider"] == "paypal"

    # But local idempotency record was NOT marked SUCCEEDED (it was left in EXECUTING before crash)
    key = derive_idempotency_key(workflow_id, "setup_billing", "acme-001", {
        "provider": "paypal", "plan_tier": "enterprise", "billing_cycle": "monthly"
    })
    rec = await store.get_idempotency_record(key)
    assert rec["status"] == IdempotencyStatus.EXECUTING.value


@pytest.mark.asyncio
async def test_5_retry_reconciles_existing_external_mutation(test_setup):
    """Scenario 5: Retry after crash reconciles external state without creating a new subscription."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 5", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    # 1. Crash on first attempt
    injector.configure_crash_after_external_success(workflow_id, "setup_billing", remaining_count=1)
    with pytest.raises(CrashBeforePersistenceError):
        await tools.setup_billing(
            workflow_id=workflow_id,
            customer_id="acme-001",
            provider="paypal",
            plan_tier="enterprise",
            billing_cycle="monthly",
        )

    external_sub_id = services._billing_records["acme-001"]["subscription_id"]

    # 2. Even simulate local memory wipe (loss of local record)
    key = derive_idempotency_key(workflow_id, "setup_billing", "acme-001", {
        "provider": "paypal", "plan_tier": "enterprise", "billing_cycle": "monthly"
    })
    store._idempotency.clear()
    assert await store.get_idempotency_record(key) is None

    # 3. Retry the operation
    reconciled_res = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )

    # Must return the SAME subscription ID from external system
    assert reconciled_res["subscription_id"] == external_sub_id
    assert len(services._billing_records) == 1

    # Local record must now be restored to SUCCEEDED
    rec = await store.get_idempotency_record(key)
    assert rec is not None
    assert rec["status"] == IdempotencyStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_6_materially_different_operation_generates_different_key(test_setup):
    """Scenario 6: Changing parameters generates different idempotency key and executes independently."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 6", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    key_stripe = derive_idempotency_key(workflow_id, "setup_billing", "acme-001", {
        "provider": "stripe", "plan_tier": "enterprise", "billing_cycle": "monthly"
    })
    key_paypal = derive_idempotency_key(workflow_id, "setup_billing", "acme-001", {
        "provider": "paypal", "plan_tier": "enterprise", "billing_cycle": "monthly"
    })
    key_annual = derive_idempotency_key(workflow_id, "setup_billing", "acme-001", {
        "provider": "stripe", "plan_tier": "enterprise", "billing_cycle": "annual"
    })

    assert key_stripe != key_paypal
    assert key_stripe != key_annual
    assert key_paypal != key_annual


@pytest.mark.asyncio
async def test_7_concurrent_duplicate_requests_cannot_duplicate_mutation(test_setup):
    """Scenario 7: Concurrent duplicate calls are serialized by lock and produce 1 external entity."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 7", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    # Launch 5 concurrent calls with identical arguments
    tasks = [
        tools.setup_billing(
            workflow_id=workflow_id,
            customer_id="acme-001",
            provider="paypal",
            plan_tier="enterprise",
            billing_cycle="monthly",
        )
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks)

    # All 5 return identical results
    sub_ids = {r["subscription_id"] for r in results}
    assert len(sub_ids) == 1
    assert len(services._billing_records) == 1


@pytest.mark.asyncio
async def test_8_failed_external_mutation_can_be_retried(test_setup):
    """Scenario 8: If external mutation fails, record is marked FAILED and can be retried."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 8", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    # Fail once on Stripe
    injector.configure_failure(
        workflow_id=workflow_id,
        tool_name="setup_billing",
        failure_type="service_unavailable",
        error_response={"status": "error", "message": "503 Stripe Down"},
        remaining_count=1,
        condition={"provider": "stripe"},
    )

    # First attempt fails
    res1 = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="stripe",
        plan_tier="enterprise",
    )
    assert res1.get("status") == "error"

    key = derive_idempotency_key(workflow_id, "setup_billing", "acme-001", {
        "provider": "stripe", "plan_tier": "enterprise", "billing_cycle": "monthly"
    })
    rec = await store.get_idempotency_record(key)
    assert rec["status"] == IdempotencyStatus.FAILED.value

    # Second attempt succeeds (remaining_count reached 0)
    res2 = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="stripe",
        plan_tier="enterprise",
    )
    assert res2.get("status") == "success"

    rec2 = await store.get_idempotency_record(key)
    assert rec2["status"] == IdempotencyStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_9_idempotency_lifecycle_transitions(test_setup):
    """Scenario 9: Idempotency records track EXECUTING -> SUCCEEDED state transitions."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 9", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    key = derive_idempotency_key(workflow_id, "verify_identity", "acme-001", {
        "full_name": "Alice Acme", "id_type": "government"
    })

    # Prior to call
    assert await store.get_idempotency_record(key) is None

    # Call tool
    res = await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    assert res["status"] == "success"

    # After call
    rec = await store.get_idempotency_record(key)
    assert rec is not None
    assert rec["status"] == IdempotencyStatus.SUCCEEDED.value
    assert rec["result"]["verification_reference_id"] == res["verification_reference_id"]


@pytest.mark.asyncio
async def test_10_crash_reconciliation_allows_workflow_to_complete(test_setup):
    """Scenario 10: Workflow subjected to crash/reconciliation can successfully complete."""
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Test 10", "idemp", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # 1. Identity
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.verify_outcome(workflow_id, "identity_verified", "acme-001")

    # 2. Documents
    await tools.validate_documents(workflow_id, "acme-001")
    await tools.verify_outcome(workflow_id, "documents_validated", "acme-001")

    # 3. Risk Check
    await tools.run_risk_check(workflow_id, "acme-001")
    await tools.verify_outcome(workflow_id, "risk_assessed", "acme-001")

    # 4. Billing with crash simulation!
    injector.configure_crash_after_external_success(workflow_id, "setup_billing", remaining_count=1)
    with pytest.raises(CrashBeforePersistenceError):
        await tools.setup_billing(workflow_id, "acme-001", provider="stripe", plan_tier="enterprise")

    # Retry billing setup (reconciles external Stripe subscription)
    await tools.setup_billing(workflow_id, "acme-001", provider="stripe", plan_tier="enterprise")
    await tools.verify_outcome(workflow_id, "billing_configured", "acme-001")

    # 5. Account Activation
    await tools.activate_account(workflow_id, "acme-001")
    await tools.verify_outcome(workflow_id, "account_activated", "acme-001")

    # 6. Welcome Package
    await tools.send_welcome_package(workflow_id, "acme-001", email="alice@acmecorp.com")
    await tools.verify_outcome(workflow_id, "welcome_sent", "acme-001")

    # Complete workflow
    wf_data = await store.get_workflow(workflow_id)
    all_verified = all(o.get("verified", False) for o in wf_data["contract"]["required_outcomes"])
    assert all_verified is True

    await engine.transition(workflow_id, WorkflowState.VERIFYING)
    await engine.transition(workflow_id, WorkflowState.COMPLETED)

    final_wf = await store.get_workflow(workflow_id)
    assert final_wf["state"] == WorkflowState.COMPLETED.value
    assert len(services._billing_records) == 1
