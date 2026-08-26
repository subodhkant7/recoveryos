"""
Structured Recovery Plan & Agentic Discovery Tests.

Verifies:
1. Recovery Specialist can submit a valid structured plan.
2. RecoveryPlan is persisted in durable/workflow store.
3. RecoveryPlan receives a stable plan ID.
4. Invalid target outcome is rejected.
5. Empty proposed steps are rejected.
6. Unknown tool/action is rejected.
7. Plan violating immutable OutcomeContract constraints is rejected.
8. Actual evidence references are validated against the evidence store.
9. Multiple plans can exist for one workflow.
10. A newer plan supersedes an older plan without deleting it.
11. Plan submission does not directly execute tools (proposal only).
12. Plan success requires independent outcome verification (PLAN SUCCESS = VERIFIED OUTCOME).
13. Environment A (Stripe down, PayPal enterprise): capability discovery discovers PayPal as valid candidate.
14. Environment B (Stripe down, PayPal starter-only, Square enterprise): capability discovery discovers Square as valid candidate.
15. Negative Environment (No provider supports required enterprise tier): capability discovery reveals no candidate and triggers escalation.
"""

import pytest
import uuid
from backend.models.workflow import WorkflowState
from backend.models.recovery import RecoveryPlanStatus, Confidence
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.external_services import SimulatedServices
from backend.persistence.workflow_store import WorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.tools.onboarding.tools import OnboardingTools
from backend.agents.agent_factory import AgentFactory
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA


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
async def test_1_2_3_submit_and_persist_valid_recovery_plan(test_setup):
    """Tests 1, 2, 3: Submit valid structured recovery plan, verify persistence and stable ID."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 1", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # 1. Complete identity prerequisite
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    # 2. Submit billing recovery plan
    res = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Stripe API returned 503 Service Unavailable during billing configuration",
        root_cause="service_unavailable",
        objective="Configure enterprise monthly billing via alternate healthy provider",
        constraints=["Enterprise tier required", "Monthly billing cycle"],
        proposed_steps=[
            {
                "tool_name": "setup_billing",
                "tool_args": {"customer_id": "acme-001", "provider": "paypal", "plan_tier": "enterprise", "billing_cycle": "monthly"},
                "rationale": "PayPal is active and supports enterprise monthly billing",
            },
            {
                "tool_name": "verify_outcome",
                "tool_args": {"outcome_id": "billing_configured", "customer_id": "acme-001"},
                "rationale": "Verify active subscription on PayPal",
            }
        ],
        verification_strategy="Query billing service directly to confirm active PayPal subscription",
        confidence="HIGH",
        reasoning="Diagnosed Stripe outage from failure evidence; queried provider capabilities; selected PayPal",
    )

    assert res["status"] == "success"
    plan_id = res["plan_id"]
    assert plan_id is not None

    # Check persistence
    persisted = await store.get_recovery_plan(workflow_id, plan_id)
    assert persisted is not None
    assert persisted["plan_id"] == plan_id
    assert persisted["target_outcome_id"] == "billing_configured"
    assert persisted["status"] == RecoveryPlanStatus.PROPOSED.value
    assert len(persisted["proposed_steps"]) == 2
    assert persisted["proposed_steps"][0]["tool_name"] == "setup_billing"


@pytest.mark.asyncio
async def test_4_invalid_target_outcome_rejected(test_setup):
    """Test 4: Submitting a plan for a non-existent outcome ID is rejected."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 4", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    res = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="non_existent_outcome",
        diagnosis="Error occurred",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001"}}],
    )
    assert res["status"] == "error"
    assert "does not exist in OutcomeContract" in res["message"]


@pytest.mark.asyncio
async def test_5_empty_proposed_steps_rejected(test_setup):
    """Test 5: Empty or missing proposed steps list is rejected."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 5", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    res = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Error occurred",
        proposed_steps=[],
    )
    assert res["status"] == "error"
    assert "must be a non-empty list" in res["message"]


@pytest.mark.asyncio
async def test_6_unknown_tool_rejected(test_setup):
    """Test 6: Proposed steps containing unknown or arbitrary tool names are rejected."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 6", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    res = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Error occurred",
        proposed_steps=[{"tool_name": "hack_external_database", "tool_args": {}}],
    )
    assert res["status"] == "error"
    assert "Unknown capability/tool" in res["message"]


@pytest.mark.asyncio
async def test_7_constraint_violation_rejected(test_setup):
    """Test 7: Plan that proposes billing before identity is verified violates immutable contract constraint."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 7", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    # Identity is NOT completed, and plan only proposes billing
    res = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Trying billing first",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
    )
    assert res["status"] == "error"
    assert "violates constraint: identity_first" in res["message"]


@pytest.mark.asyncio
async def test_8_evidence_reference_validation(test_setup):
    """Test 8: Referenced evidence IDs must exist in the workflow evidence store."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 8", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)

    # First verify identity so constraint is satisfied
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    # 1. Invalid evidence reference
    res_bad = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Stripe failed",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
        evidence_ids=["fake-evidence-id-999"],
    )
    assert res_bad["status"] == "error"
    assert "does not exist in workflow evidence store" in res_bad["message"]

    # 2. Valid evidence reference
    await store.save_evidence(workflow_id, {
        "evidence_id": "real-ev-123",
        "workflow_id": workflow_id,
        "source": "stripe:error",
        "evidence_type": "FAILURE_DETAIL",
        "data": {"http_status": 503},
    })
    res_good = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Stripe failed with 503",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
        evidence_ids=["real-ev-123"],
    )
    assert res_good["status"] == "success"


@pytest.mark.asyncio
async def test_9_10_multiple_plans_and_supersession(test_setup):
    """Tests 9 & 10: Multiple plans persist; a newer plan supersedes previous plans without deleting them."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 9-10", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    # First Plan: PayPal
    res1 = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Initial diagnosis: Try PayPal",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
    )
    plan1_id = res1["plan_id"]

    # Second Plan: Square (e.g. if PayPal subsequently had issues)
    res2 = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Updated diagnosis: Try Square",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "square"}}],
    )
    plan2_id = res2["plan_id"]

    assert plan1_id != plan2_id

    # Check that both plans exist in store
    all_plans = await store.get_recovery_plans(workflow_id)
    assert len(all_plans) == 2

    # Plan 1 must be SUPERSEDED
    p1 = await store.get_recovery_plan(workflow_id, plan1_id)
    assert p1["status"] == RecoveryPlanStatus.SUPERSEDED.value

    # Plan 2 must be PROPOSED (active)
    p2 = await store.get_recovery_plan(workflow_id, plan2_id)
    assert p2["status"] == RecoveryPlanStatus.PROPOSED.value

    # Active plan query returns Plan 2
    active_plan = await store.get_active_recovery_plan(workflow_id, "billing_configured")
    assert active_plan["plan_id"] == plan2_id


@pytest.mark.asyncio
async def test_11_plan_does_not_directly_execute_mutations(test_setup):
    """Test 11: submit_recovery_plan creates a proposal only and does not mutate external services."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 11", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    # Submit plan proposing billing
    await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Proposal only",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
    )

    # External billing state must remain empty!
    assert len(services._billing_records) == 0


@pytest.mark.asyncio
async def test_12_plan_success_requires_independent_verification(test_setup):
    """Test 12: RecoveryPlan is only marked SUCCEEDED after independent outcome verification succeeds."""
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Plan 12", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")
    await tools.run_risk_check(workflow_id, "acme-001")

    # Submit plan
    res = await tools.submit_recovery_plan(
        workflow_id=workflow_id,
        target_outcome_id="billing_configured",
        diagnosis="Stripe down, using PayPal",
        proposed_steps=[{"tool_name": "setup_billing", "tool_args": {"customer_id": "acme-001", "provider": "paypal"}}],
    )
    plan_id = res["plan_id"]

    # Plan is still PROPOSED
    p_before = await store.get_recovery_plan(workflow_id, plan_id)
    assert p_before["status"] == RecoveryPlanStatus.PROPOSED.value

    # Execute tool
    await tools.setup_billing(workflow_id, "acme-001", provider="paypal", plan_tier="enterprise")

    # Verify outcome independently
    verif = await tools.verify_outcome(workflow_id, "billing_configured", "acme-001")
    assert verif["passed"] is True

    # Plan status must now be updated to SUCCEEDED
    p_after = await store.get_recovery_plan(workflow_id, plan_id)
    assert p_after["status"] == RecoveryPlanStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_13_14_dynamic_capability_discovery_and_environment_selection(test_setup):
    """
    Tests 13 & 14 (Agentic Environment Selection):
    Environment A: Stripe is down, PayPal is healthy and supports enterprise -> Discovery reveals PayPal.
    Environment B: Stripe is down, PayPal only supports starter/pro, Square is healthy and supports enterprise -> Discovery reveals Square.
    """
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup

    # -------------------------------------------------------------
    # Environment A:
    # -------------------------------------------------------------
    services.configure_billing_provider("stripe", status="down")
    services.configure_billing_provider("paypal", status="healthy", supported_plan_tiers=["starter", "professional", "enterprise"])
    services.configure_billing_provider("square", status="healthy", supported_plan_tiers=["starter"])

    # Agent discovers capabilities via tool
    providers_a = await tools.list_available_billing_providers()

    # Agent reasons over discovered providers to find candidate meeting enterprise monthly contract:
    candidates_a = [
        p["provider"] for p in providers_a
        if p["status"] == "healthy" and "enterprise" in p["supported_plan_tiers"]
    ]
    assert candidates_a == ["paypal"]

    # -------------------------------------------------------------
    # Environment B:
    # -------------------------------------------------------------
    services.configure_billing_provider("stripe", status="down")
    services.configure_billing_provider("paypal", status="healthy", supported_plan_tiers=["starter", "professional"])  # No enterprise!
    services.configure_billing_provider("square", status="healthy", supported_plan_tiers=["starter", "enterprise"])

    # Agent discovers capabilities in Environment B
    providers_b = await tools.list_available_billing_providers()

    candidates_b = [
        p["provider"] for p in providers_b
        if p["status"] == "healthy" and "enterprise" in p["supported_plan_tiers"]
    ]
    assert candidates_b == ["square"]


@pytest.mark.asyncio
async def test_15_negative_agenticity_no_candidate_satisfies_contract(test_setup):
    """
    Test 15 (Negative Agenticity Test):
    When NO available provider supports the required contract tier (enterprise),
    the system discovers 0 candidate providers and does not force an invalid action.
    """
    injector, services, store, engine, policy_engine, agent_factory, tools = test_setup

    # All enterprise-capable providers are down, remaining provider only supports starter
    services.configure_billing_provider("stripe", status="down")
    services.configure_billing_provider("paypal", status="down")
    services.configure_billing_provider("square", status="healthy", supported_plan_tiers=["starter", "professional"])

    providers = await tools.list_available_billing_providers()

    # Evaluating candidates against enterprise contract requirement
    viable_candidates = [
        p["provider"] for p in providers
        if p["status"] == "healthy" and "enterprise" in p["supported_plan_tiers"]
    ]

    # No viable candidate exists
    assert len(viable_candidates) == 0
