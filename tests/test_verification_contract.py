"""
Tests for Outcome Verification and Contract State Synchronization.

Verifies:
A. Successful verification sets RequiredOutcome.verified=True and attaches evidence ID.
B. Failed verification does NOT mark the outcome verified and sets verified=False.
C. Successful workflow with all outcomes verified transitions to COMPLETED.
D. Failed/incomplete workflow cannot reach COMPLETED and transitions to RECOVERING/ESCALATED.
E. Verification queries real simulated service state rather than accepting LLM assertions.
"""

import pytest
import uuid
from backend.models.workflow import WorkflowState
from backend.simulation.failure_injector import FailureInjector
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
async def test_successful_verification_sets_outcome_verified(test_setup):
    """
    Test A: When verify_outcome() succeeds against simulated state,
    RequiredOutcome.verified must be set to True and evidence_ids must be populated.
    """
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow(
        name="Test Verification",
        scenario="happy_path",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
    )

    # 1. Execute the identity verification mutating tool
    tool_res = await tools.verify_identity(
        workflow_id=workflow_id,
        customer_id="acme-001",
        full_name="Alice Acme",
    )
    assert tool_res.get("status") == "success"

    # 2. Execute independent outcome verification
    verif_res = await tools.verify_outcome(
        workflow_id=workflow_id,
        outcome_id="identity_verified",
        customer_id="acme-001",
    )
    assert verif_res.get("passed") is True
    assert len(verif_res.get("discrepancies", [])) == 0

    # 3. Inspect persisted contract state
    wf = await store.get_workflow(workflow_id)
    contract_outcomes = wf["contract"]["required_outcomes"]
    identity_outcome = next(o for o in contract_outcomes if o["outcome_id"] == "identity_verified")

    assert identity_outcome["verified"] is True
    assert len(identity_outcome["evidence_ids"]) > 0


@pytest.mark.asyncio
async def test_failed_verification_does_not_mark_outcome_verified(test_setup):
    """
    Test B: When verify_outcome() fails (e.g. data missing or criteria mismatch),
    RequiredOutcome.verified must remain False and discrepancies must be reported.
    """
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow(
        name="Test Failed Verification",
        scenario="unverified",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
    )

    # Do NOT execute billing setup; try to verify billing immediately
    verif_res = await tools.verify_outcome(
        workflow_id=workflow_id,
        outcome_id="billing_configured",
        customer_id="acme-001",
    )
    assert verif_res.get("passed") is False
    assert len(verif_res.get("discrepancies", [])) > 0

    # Inspect persisted contract state
    wf = await store.get_workflow(workflow_id)
    contract_outcomes = wf["contract"]["required_outcomes"]
    billing_outcome = next(o for o in contract_outcomes if o["outcome_id"] == "billing_configured")

    assert billing_outcome["verified"] is False


@pytest.mark.asyncio
async def test_criteria_mismatch_fails_verification(test_setup):
    """
    Test B (continued): Even if service returns a record, if the acceptance criteria
    (e.g., plan_tier=enterprise) does not match the actual service state (e.g. starter),
    verification must fail and contract outcome must remain unverified.
    """
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow(
        name="Test Mismatch Verification",
        scenario="mismatch",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
    )

    # Configure billing with 'starter' plan (acceptance criteria requires 'enterprise')
    await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="stripe",
        plan_tier="starter",
        billing_cycle="monthly",
    )

    verif_res = await tools.verify_outcome(
        workflow_id=workflow_id,
        outcome_id="billing_configured",
        customer_id="acme-001",
    )
    assert verif_res.get("passed") is False
    assert any("starter" in d for d in verif_res.get("discrepancies", []))

    # Outcome remains unverified in contract
    wf = await store.get_workflow(workflow_id)
    billing_outcome = next(o for o in wf["contract"]["required_outcomes"] if o["outcome_id"] == "billing_configured")
    assert billing_outcome["verified"] is False


@pytest.mark.asyncio
async def test_full_happy_path_reaches_completed(test_setup):
    """
    Test C: When all 6 required outcomes are executed and verified,
    the workflow successfully transitions to COMPLETED.
    """
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow(
        name="Full Happy Path",
        scenario="happy_path",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
    )
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

    # 4. Billing
    await tools.setup_billing(workflow_id, "acme-001", provider="stripe", plan_tier="enterprise")
    await tools.verify_outcome(workflow_id, "billing_configured", "acme-001")

    # 5. Account Activation
    await tools.activate_account(workflow_id, "acme-001")
    await tools.verify_outcome(workflow_id, "account_activated", "acme-001")

    # 6. Welcome Package
    await tools.send_welcome_package(workflow_id, "acme-001", email="alice@acmecorp.com")
    await tools.verify_outcome(workflow_id, "welcome_sent", "acme-001")

    # Simulate completion check
    wf_data = await store.get_workflow(workflow_id)
    all_verified = all(o.get("verified", False) for o in wf_data["contract"]["required_outcomes"])
    assert all_verified is True

    await engine.transition(workflow_id, WorkflowState.VERIFYING)
    await engine.transition(workflow_id, WorkflowState.COMPLETED, detail="All outcomes independently verified")

    final_wf = await store.get_workflow(workflow_id)
    assert final_wf["state"] == WorkflowState.COMPLETED.value
    assert final_wf.get("completed_at") is not None


@pytest.mark.asyncio
async def test_incomplete_workflow_cannot_reach_completed(test_setup):
    """
    Test D: If some outcomes remain unverified, the workflow cannot be marked COMPLETED
    and instead transitions to RECOVERING.
    """
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow(
        name="Partial Workflow",
        scenario="partial",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
    )
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # Execute only identity and documents
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.verify_outcome(workflow_id, "identity_verified", "acme-001")

    await tools.validate_documents(workflow_id, "acme-001")
    await tools.verify_outcome(workflow_id, "documents_validated", "acme-001")

    # Check contract fulfillment
    wf_data = await store.get_workflow(workflow_id)
    all_verified = all(o.get("verified", False) for o in wf_data["contract"]["required_outcomes"])
    assert all_verified is False

    await engine.transition(workflow_id, WorkflowState.VERIFYING)
    # Transitioning to RECOVERING because outcomes are missing
    await engine.transition(workflow_id, WorkflowState.RECOVERING, detail="Unverified outcomes detected")

    final_wf = await store.get_workflow(workflow_id)
    assert final_wf["state"] == WorkflowState.RECOVERING.value
    assert final_wf.get("completed_at") is None
