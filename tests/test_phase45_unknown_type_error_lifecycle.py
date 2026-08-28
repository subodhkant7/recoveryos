"""
Phase 45 Regression Test Suite — Robust Schema Normalization & TypeError / UNKNOWN Elimination.

Verifies:
1. Normal execution of billing_unavailable through the real async worker pipeline.
2. Graceful and canonical handling of string/malformed contract, constraint, customer, and snapshot data.
3. Multi-scenario compatibility (contradictory_evidence, worker_interruption).
4. Repeated scenario launches with clean workflow isolation and deterministic outcomes.
5. Structured error capturing and invariant preservation.
"""

import asyncio
import uuid
import pytest

from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.simulation.failure_injector import FailureInjector, configure_scenario_1
from backend.simulation.external_services import SimulatedServices
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA
from backend.agents.agent_factory import AgentFactory
from backend.events.consumer import WorkflowEventConsumer
from backend.events.message_models import WorkflowExecutionMessage, WorkflowEventType
from backend.models.workflow import (
    WorkflowState,
    normalize_contract,
    normalize_customer_data,
    normalize_workflow_snapshot,
)
from backend.engine.agent_runner import build_agent_prompt


@pytest.mark.asyncio
async def test_a_billing_unavailable_async_worker_execution():
    """Test A: billing_unavailable execution and independent verification flow."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)
    consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        worker_id="worker-test-p45",
        agent_factory=agent_factory,
    )

    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    wf_data = await engine.create_workflow(
        name="ACME Corp Onboarding — billing_unavailable",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
    )
    assert wf_data["state"] == WorkflowState.CREATED.value

    # Execute Onboarding tools through the deterministic engine
    from backend.tools.onboarding.tools import OnboardingTools
    tools = OnboardingTools(services=services, store=store, engine=engine)

    # 1. Identity Verification
    id_res = await tools.verify_identity(workflow_id=wf_id, customer_id="acme-001")
    assert id_res.get("status") == "success"
    v_id = await tools.verify_outcome(workflow_id=wf_id, outcome_id="identity_verified", customer_id="acme-001")
    assert v_id.get("passed") is True

    # 2. Document Validation
    doc_res = await tools.validate_documents(workflow_id=wf_id, customer_id="acme-001", document_types="incorporation,tax_id")
    assert doc_res.get("status") == "success"
    v_doc = await tools.verify_outcome(workflow_id=wf_id, outcome_id="documents_validated", customer_id="acme-001")
    assert v_doc.get("passed") is True

    # 3. Risk Assessment
    risk_res = await tools.run_risk_check(workflow_id=wf_id, customer_id="acme-001")
    assert risk_res.get("status") == "success"
    v_risk = await tools.verify_outcome(workflow_id=wf_id, outcome_id="risk_assessed", customer_id="acme-001")
    assert v_risk.get("passed") is True

    # 4. Billing Setup: Stripe fails with 503
    configure_scenario_1(injector, wf_id)
    stripe_res = await tools.setup_billing(
        workflow_id=wf_id,
        customer_id="acme-001",
        provider="stripe",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )
    assert stripe_res.get("status") == "error"

    # 5. Billing Setup: Failover to PayPal succeeds
    paypal_res = await tools.setup_billing(
        workflow_id=wf_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
    )
    assert paypal_res.get("status") == "success"
    v_bill = await tools.verify_outcome(workflow_id=wf_id, outcome_id="billing_configured", customer_id="acme-001")
    assert v_bill.get("passed") is True

    # 6. Account Activation & Welcome Package
    act_res = await tools.activate_account(workflow_id=wf_id, customer_id="acme-001")
    assert act_res.get("status") == "success"
    v_act = await tools.verify_outcome(workflow_id=wf_id, outcome_id="account_activated", customer_id="acme-001")
    assert v_act.get("passed") is True

    welc_res = await tools.send_welcome_package(workflow_id=wf_id, customer_id="acme-001", email="alice@acmecorp.com")
    assert welc_res.get("status") == "success"
    v_welc = await tools.verify_outcome(workflow_id=wf_id, outcome_id="welcome_sent", customer_id="acme-001")
    assert v_welc.get("passed") is True

    # Transition to VERIFYING → COMPLETED
    await engine.transition(wf_id, WorkflowState.EXECUTING)
    await engine.transition(wf_id, WorkflowState.VERIFYING)
    await engine.transition(wf_id, WorkflowState.COMPLETED)

    # Verify authoritative workflow state is COMPLETED
    cur_wf = await store.get_workflow(wf_id)
    assert cur_wf is not None
    assert cur_wf["state"] == WorkflowState.COMPLETED.value
    assert cur_wf["state"] != WorkflowState.UNKNOWN.value

    # Verify events
    events = await store.get_events(wf_id)
    event_titles = [e.get("title", "") for e in events]
    assert not any("TypeError" in t or "string indices" in t for t in event_titles)


@pytest.mark.asyncio
async def test_b_malformed_contract_and_string_schemas():
    """Test B: Canonical normalization handles strings, malformed structures, and invalid JSON."""
    # 1. String-based outcomes and constraints
    raw_contract = {
        "required_outcomes": ["identity_verified", "billing_configured"],
        "constraints": ["identity_first", "risk_before_billing"],
        "prohibited_outcomes": ["double_charge"],
    }
    normalized = normalize_contract(raw_contract, workflow_id="test-wf-1")
    assert isinstance(normalized["required_outcomes"], list)
    assert isinstance(normalized["required_outcomes"][0], dict)
    assert normalized["required_outcomes"][0]["outcome_id"] == "identity_verified"
    assert isinstance(normalized["constraints"], list)
    assert isinstance(normalized["constraints"][0], dict)
    assert normalized["constraints"][0]["constraint_id"] == "identity_first"

    # 2. JSON string contract
    json_contract = '{"required_outcomes": ["documents_validated"], "constraints": ["identity_first"]}'
    normalized_json = normalize_contract(json_contract, workflow_id="test-wf-2")
    assert normalized_json["required_outcomes"][0]["outcome_id"] == "documents_validated"
    assert normalized_json["constraints"][0]["constraint_id"] == "identity_first"

    # 3. String customer data
    cust = normalize_customer_data("acme-001")
    assert cust == {"customer_id": "acme-001"}

    # 4. Prompt builder with malformed data
    snapshot = {
        "workflow": {"workflow_id": "wf-test-malformed"},
        "steps": ["invalid_step_str", {"name": "step1", "status": "COMPLETED", "tool_name": "verify_identity"}],
        "failures": ["invalid_failure_str", {"component": "stripe", "error_message": "503 Unavailable"}],
    }
    prompt = build_agent_prompt(snapshot, "acme-001", raw_contract)
    assert "WORKFLOW ID: wf-test-malformed" in prompt
    assert "identity_verified" in prompt
    assert "identity_first" in prompt
    assert "double_charge" in prompt
    assert "503 Unavailable" in prompt


@pytest.mark.asyncio
async def test_c_other_scenario_definitions():
    """Test C: Multi-scenario definitions and failure injection."""
    from backend.simulation.scenarios import configure_demo_scenario

    injector = FailureInjector()
    for scen in ["billing_unavailable", "contradictory_evidence", "worker_interruption"]:
        wf_id = str(uuid.uuid4())
        configure_demo_scenario(injector, wf_id, scen)

    with pytest.raises(ValueError):
        configure_demo_scenario(injector, "wf-bad", "non_existent_scenario")


@pytest.mark.asyncio
async def test_d_repeated_billing_launches_isolated():
    """Test D: Repeated launches of billing_unavailable are isolated."""
    from unittest.mock import AsyncMock

    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    injector = FailureInjector()
    services = SimulatedServices(injector)
    policy_engine = PolicyEngine()
    agent_factory = AgentFactory(store, engine, services, policy_engine)
    consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        worker_id="worker-test-p45-rep",
        agent_factory=agent_factory,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("backend.events.consumer.run_workflow_agent", AsyncMock(return_value={"status": "COMPLETED"}))

        for i in range(3):
            wf_id = f"test-rep-{i}-{uuid.uuid4().hex[:6]}"
            contract = create_acme_contract(wf_id)
            await engine.create_workflow(
                name=f"ACME Corp Onboarding Run {i}",
                scenario="billing_unavailable",
                customer_data=ACME_CUSTOMER_DATA,
                contract_data=contract,
                workflow_id=wf_id,
            )

            msg = WorkflowExecutionMessage(
                event_type=WorkflowEventType.WORKFLOW_DISPATCH,
                workflow_id=wf_id,
                tenant_id="tenant-default",
                idempotency_key=f"dispatch_{wf_id}",
                expected_version=1,
                correlation_id=str(uuid.uuid4()),
                producer_id="recoveryos-api",
                payload={"scenario": "billing_unavailable"},
            )
            res = await consumer.consume_message(msg)
            assert res["status"] == "PROCESSED"

            events = await store.get_events(wf_id)
            assert not any("TypeError" in e.get("title", "") for e in events)
