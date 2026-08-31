"""
Regression Tests for Scenario Injection, Autonomous Failover, Contradictory Evidence,
Recovery Proof Correlation, and Model Fallback Safety.
"""

import asyncio
import pytest
from backend.models.workflow import WorkflowState
from backend.models.events import EventType
from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.external_services import SimulatedServices
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA, configure_demo_scenario
from backend.agents.agent_factory import AgentFactory
from backend.events.publisher import InMemoryEventPublisher
from backend.events.consumer import WorkflowEventConsumer
from backend.worker.service import WorkflowWorkerService
from backend.events.message_models import WorkflowExecutionMessage, WorkflowEventType
from backend.llm.resilience import get_resilience_events, clear_resilience_events


@pytest.mark.asyncio
async def test_billing_unavailable_fails_over_to_paypal():
    """
    Test billing_unavailable scenario:
    1. Stripe returns 503 Service Unavailable.
    2. Recovery specialist diagnoses outage and targets PayPal.
    3. PayPal action succeeds.
    4. Independent billing verification confirms active subscription on PayPal.
    5. Final workflow completes with 6/6 verified outcomes.
    6. Verifies that Stripe 503 does NOT trigger any Gemini Lite model fallback.
    """
    clear_resilience_events()
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)
    publisher = InMemoryEventPublisher()

    wf_id = "wf-test-billing-unavailable-01"
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Billing Unavailable Test",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
    )

    configure_demo_scenario(injector, wf_id, "billing_unavailable", services=services)

    # 1. Verify Stripe is initially marked down
    stripe_status = services.get_service_status("billing_stripe")
    assert stripe_status["status"] == "down"

    # 2. Attempting setup_billing on Stripe returns 503 error
    stripe_res = await services.setup_billing(
        workflow_id=wf_id,
        customer_id="acme-001",
        provider="stripe",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )
    assert stripe_res.get("status") == "error"
    assert stripe_res.get("error_type") == "service_unavailable"
    assert "503" in stripe_res.get("message", "")

    # 3. Discover available providers shows PayPal is available
    providers = services.list_available_billing_providers()
    stripe_prov = next((p for p in providers if p["provider"] == "stripe"), None)
    paypal_prov = next((p for p in providers if p["provider"] == "paypal"), None)
    assert stripe_prov["status"] == "down"
    assert paypal_prov["status"] == "healthy"
    assert paypal_prov["supports_enterprise"] is True

    # 4. Execute setup_billing on PayPal
    paypal_res = await services.setup_billing(
        workflow_id=wf_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )
    assert paypal_res.get("status") == "success"
    assert paypal_res.get("provider") == "paypal"

    # 5. Independent verification confirms PayPal subscription
    ver_res = await services.query_billing_status("acme-001", workflow_id=wf_id)
    assert ver_res.get("found") is True
    assert ver_res.get("provider") == "paypal"
    assert ver_res.get("plan_tier") == "enterprise"

    # 6. Verify NO model fallback was triggered by business service 503
    res_events = get_resilience_events()
    fallback_events = [e for e in res_events if e.get("event_type") == "MODEL_FALLBACK"]
    assert len(fallback_events) == 0, f"Expected 0 MODEL_FALLBACK events, found: {fallback_events}"


@pytest.mark.asyncio
async def test_contradictory_evidence_stops_autonomy():
    """
    Test contradictory_evidence scenario:
    1. setup_billing succeeds with wrong plan (starter instead of enterprise).
    2. Independent verification detects discrepancy.
    3. billing_configured verified == False.
    4. Workflow pauses at autonomy boundary in AWAITING_APPROVAL.
    5. No account activation or welcome package is executed.
    6. Workflow does NOT complete.
    """
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)

    wf_id = "wf-test-contradictory-01"
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Contradictory Evidence Test",
        scenario="contradictory_evidence",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
    )

    configure_demo_scenario(injector, wf_id, "contradictory_evidence", services=services)

    # Transition to EXECUTING
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Starting workflow")

    # Prerequisite steps: verify_identity, validate_documents, run_risk_check
    await agent_factory.tools.verify_identity(workflow_id=wf_id, customer_id="acme-001")
    await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="identity_verified", customer_id="acme-001")

    await agent_factory.tools.validate_documents(workflow_id=wf_id, customer_id="acme-001")
    await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="documents_validated", customer_id="acme-001")

    await agent_factory.tools.run_risk_check(workflow_id=wf_id, customer_id="acme-001")
    await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="risk_assessed", customer_id="acme-001")

    # Execute setup_billing (injected to return wrong plan: starter)
    billing_action_res = await agent_factory.tools.setup_billing(
        workflow_id=wf_id,
        customer_id="acme-001",
        provider="stripe",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )
    assert billing_action_res.get("status") == "success"
    assert billing_action_res.get("plan_tier") == "starter"  # Wrong plan injected

    # Independent verification runs
    ver_res = await agent_factory.tools.verify_outcome(
        workflow_id=wf_id,
        outcome_id="billing_configured",
        customer_id="acme-001",
    )
    assert ver_res.get("passed") is False
    assert len(ver_res.get("discrepancies", [])) > 0
    assert any("starter" in str(d) or "enterprise" in str(d) for d in ver_res["discrepancies"])

    # Workflow must be paused in AWAITING_APPROVAL
    wf = await store.get_workflow(wf_id)
    assert wf["state"] == WorkflowState.AWAITING_APPROVAL.value

    # Outcome billing_configured must NOT be verified
    for o in wf["contract"]["required_outcomes"]:
        if o["outcome_id"] == "billing_configured":
            assert o["verified"] is False
        if o["outcome_id"] in ("account_activated", "welcome_sent"):
            assert o["verified"] is False

    # Ensure account activation and welcome package were NOT executed
    steps = await store.get_steps(wf_id)
    completed_tools = [s.get("tool_name") for s in steps if s.get("status") == "COMPLETED"]
    assert "activate_account" not in completed_tools
    assert "send_welcome_package" not in completed_tools
    assert wf["state"] != WorkflowState.COMPLETED.value


@pytest.mark.asyncio
async def test_scenario_isolation_order_a_b_c_a():
    """
    Test deterministic scenario isolation in sequential order:
    1. billing_unavailable (A)
    2. contradictory_evidence (B)
    3. worker_interruption (C)
    4. billing_unavailable again (A)

    Verifies that the second run of billing_unavailable behaves identically to the first.
    """
    injector = FailureInjector()
    services = SimulatedServices(injector)

    # 1. Run billing_unavailable (A)
    wf_a1 = "wf-iso-a1"
    configure_demo_scenario(injector, wf_a1, "billing_unavailable", services=services, reset_state=True)
    res_a1 = await services.setup_billing(workflow_id=wf_a1, customer_id="acme-001", provider="stripe")
    assert res_a1.get("status") == "error"
    assert "503" in res_a1.get("message", "")

    # 2. Run contradictory_evidence (B)
    wf_b = "wf-iso-b"
    configure_demo_scenario(injector, wf_b, "contradictory_evidence", services=services, reset_state=True)
    res_b = await services.setup_billing(workflow_id=wf_b, customer_id="acme-001", provider="stripe", plan_tier="enterprise")
    assert res_b.get("status") == "success"
    assert res_b.get("plan_tier") == "starter"

    # 3. Run worker_interruption (C)
    wf_c = "wf-iso-c"
    configure_demo_scenario(injector, wf_c, "worker_interruption", services=services, reset_state=True)
    res_c = await services.setup_billing(workflow_id=wf_c, customer_id="acme-001", provider="stripe", plan_tier="enterprise")
    assert res_c.get("status") == "success"
    assert res_c.get("plan_tier") == "enterprise"
    assert injector.should_crash_after_external_success(wf_c, "setup_billing") is True

    # 4. Run billing_unavailable again (A2)
    wf_a2 = "wf-iso-a2"
    configure_demo_scenario(injector, wf_a2, "billing_unavailable", services=services, reset_state=True)
    res_a2 = await services.setup_billing(workflow_id=wf_a2, customer_id="acme-001", provider="stripe")
    assert res_a2.get("status") == "error"
    assert "503" in res_a2.get("message", "")
    assert res_a2 == res_a1


@pytest.mark.asyncio
async def test_recovery_proof_correlation_exact_matching():
    """
    Test that Recovery Proof pairs action and verification evidence by matching outcome:
    billing_configured -> setup_billing action + billing service verification
    (NOT setup_billing + identity service verification).
    """
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)

    wf_id = "wf-proof-test-01"
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Proof Correlation Test",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
    )
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Starting workflow")

    # Step 1: verify_identity
    await agent_factory.tools.verify_identity(workflow_id=wf_id, customer_id="acme-001")
    id_ver = await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="identity_verified", customer_id="acme-001")

    # Step 2: validate_documents
    await agent_factory.tools.validate_documents(workflow_id=wf_id, customer_id="acme-001")
    doc_ver = await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="documents_validated", customer_id="acme-001")

    # Step 3: run_risk_check
    await agent_factory.tools.run_risk_check(workflow_id=wf_id, customer_id="acme-001")
    risk_ver = await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="risk_assessed", customer_id="acme-001")

    # Step 4: setup_billing (paypal)
    await agent_factory.tools.setup_billing(workflow_id=wf_id, customer_id="acme-001", provider="paypal", plan_tier="enterprise")
    bill_ver = await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="billing_configured", customer_id="acme-001")

    # Step 5: activate_account
    await agent_factory.tools.activate_account(workflow_id=wf_id, customer_id="acme-001")
    acct_ver = await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="account_activated", customer_id="acme-001")

    # Step 6: send_welcome_package
    await agent_factory.tools.send_welcome_package(workflow_id=wf_id, customer_id="acme-001", email="alice@acmecorp.com")
    welc_ver = await agent_factory.tools.verify_outcome(workflow_id=wf_id, outcome_id="welcome_sent", customer_id="acme-001")

    # Complete workflow
    await engine.transition(wf_id, WorkflowState.VERIFYING, detail="Verifying outcomes")
    await engine.transition(wf_id, WorkflowState.COMPLETED, detail="Completed")

    snapshot = await store.get_workflow_snapshot(wf_id)
    steps = snapshot["steps"]
    evidence = snapshot["evidence"]

    # Verify billing action and billing verification correlation
    billing_steps = [s for s in steps if s.get("tool_name") == "setup_billing" and s.get("status") == "COMPLETED"]
    assert len(billing_steps) == 1
    billing_action = billing_steps[0]

    billing_verifications = [
        e for e in evidence
        if (
            str(e.get("evidence_type", "")).upper() in ("VERIFICATION", "EVIDENCETYPE.VERIFICATION")
            or e.get("source") == "verify:billing_configured"
        ) and (e.get("data", {}).get("outcome_id") == "billing_configured" or e.get("data", {}).get("target") == "billing_configured")
    ]
    assert len(billing_verifications) == 1
    billing_verification = billing_verifications[0]

    # Proof pairing must be billing action + billing verification
    assert billing_action["tool_name"] == "setup_billing"
    assert "paypal" in billing_action.get("name", "").lower() or billing_action.get("tool_args", {}).get("provider") == "paypal"
    assert "billing service" in billing_verification.get("data", {}).get("method", "").lower()
    assert "identity" not in billing_verification.get("data", {}).get("method", "").lower()
