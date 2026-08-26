"""
Tests for Workflow Step Tracking Lifecycle.

Verifies:
E. Every tool action creates a persistent WorkflowStep.
   - Transitions START -> RUNNING -> COMPLETED on success.
   - Transitions START -> RUNNING -> FAILED on error.
   - Captures tool name, arguments, timestamps, and evidence references.
"""

import pytest
import uuid
from backend.models.workflow import StepStatus
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
async def test_step_tracking_on_success(test_setup):
    """
    Test E (success path): Invoking a tool creates a WorkflowStep in RUNNING state,
    and transitions it to COMPLETED with started_at, completed_at, and evidence_id upon success.
    """
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow(
        name="Step Tracking Success",
        scenario="step_test",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
    )

    # Execute identity tool
    res = await tools.verify_identity(
        workflow_id=workflow_id,
        customer_id="acme-001",
        full_name="Alice Acme",
    )
    assert res.get("status") == "success"

    steps = await store.get_steps(workflow_id)
    assert len(steps) == 1

    step = steps[0]
    assert step["tool_name"] == "verify_identity"
    assert step["target_outcome_id"] == "identity_verified"
    assert step["status"] == StepStatus.COMPLETED.value
    assert step.get("started_at") is not None
    assert step.get("completed_at") is not None
    assert step.get("evidence_id") is not None
    assert step.get("result", {}).get("status") == "success"

    # Verify evidence was recorded
    evidence = await store.get_evidence(workflow_id, step["evidence_id"])
    assert evidence is not None
    assert evidence["source"] == "verify_identity"


@pytest.mark.asyncio
async def test_step_tracking_on_failure(test_setup):
    """
    Test E (failure path): When a tool fails (e.g. simulated outage), the step
    is marked as FAILED with error message and a Failure record is created.
    """
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow(
        name="Step Tracking Failure",
        scenario="failure_test",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
    )

    # Configure failure on setup_billing
    injector.configure_failure(
        workflow_id=workflow_id,
        tool_name="setup_billing",
        failure_type="service_unavailable",
        error_response={
            "status": "error",
            "error_type": "service_unavailable",
            "message": "Stripe API 503 Outage",
        },
        condition={"provider": "stripe"},
    )

    # Execute billing setup targeting Stripe
    res = await tools.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="stripe",
        plan_tier="enterprise",
    )
    assert res.get("status") == "error"

    steps = await store.get_steps(workflow_id)
    assert len(steps) == 1

    step = steps[0]
    assert step["tool_name"] == "setup_billing"
    assert step["status"] == StepStatus.FAILED.value
    assert "503 Outage" in step["error"]
    assert step.get("completed_at") is not None

    # Check failure record
    failures = await store.get_failures(workflow_id)
    assert len(failures) == 1
    assert failures[0]["step_id"] == step["step_id"]
    assert failures[0]["error_type"] == "service_unavailable"


@pytest.mark.asyncio
async def test_timeline_events_recorded_for_steps(test_setup):
    """
    Test E (events): Workflow engine records immutable timeline events
    for every step started, completed, or failed.
    """
    injector, services, store, engine, tools = test_setup
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    await engine.create_workflow(
        name="Timeline Events Test",
        scenario="events_test",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
    )

    # 1. Run a successful step
    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    # 2. Inspect timeline events
    events = await store.get_events(workflow_id)
    event_types = [e["event_type"] for e in events]

    assert "STATE_CHANGE" in event_types  # Workflow created
    assert "STEP_STARTED" in event_types
    assert "STEP_COMPLETED" in event_types
