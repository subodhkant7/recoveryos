"""Regression coverage for claims made in the hackathon submission."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend.agents.agent_factory import AgentFactory
from backend.engine.agent_runner import run_workflow_agent
from backend.engine.policy_engine import PolicyEngine
from backend.engine.workflow_engine import WorkflowEngine
from backend.models.workflow import WorkflowState
from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.simulation.external_services import SimulatedServices
from backend.simulation.failure_injector import (
    CrashBeforePersistenceError,
    FailureInjector,
)
from backend.simulation.scenarios import (
    ACME_CUSTOMER_DATA,
    configure_demo_scenario,
    create_acme_contract,
)


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_model_and_adk_wrapper_use_canonical_configuration():
    """Gemini model selection remains centralized and ADK-backed."""
    config_source = (ROOT / "backend/config.py").read_text()
    resilience_source = (ROOT / "backend/llm/resilience.py").read_text()
    factory_source = (ROOT / "backend/agents/agent_factory.py").read_text()
    runner_source = (ROOT / "backend/engine/agent_runner.py").read_text()

    # Runtime settings are intentionally environment-overridable. Protect the
    # canonical fallback rather than treating a developer's local override as
    # a failure of the deployed configuration.
    assert 'os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")' in config_source
    assert "class ResilientGemini(Gemini)" in resilience_source
    assert "model_name = model or config.gemini_model" in resilience_source
    assert "ResilientGemini(model=config.gemini_model" in factory_source
    assert "from google.adk.runners import Runner" in runner_source


def test_worker_interruption_scenario_is_one_time_and_deterministic():
    """A retry does not re-inject a crash after the first interrupted write."""
    injector = FailureInjector()
    workflow_id = "wf-worker-interruption"

    configure_demo_scenario(injector, workflow_id, "worker_interruption")
    configure_demo_scenario(injector, workflow_id, "worker_interruption")

    key = (workflow_id, "setup_billing")
    assert len(injector._crash_configs[key]) == 1
    assert injector.should_crash_after_external_success(*key) is True
    assert injector.should_crash_after_external_success(*key) is False


@pytest.mark.asyncio
async def test_worker_interruption_reconciles_external_write_then_redispatches(
    monkeypatch: pytest.MonkeyPatch,
):
    """A post-write interruption cannot duplicate billing before recovery retries."""
    workflow_id = "wf-worker-interruption-reconcile"
    injector = FailureInjector()
    services = SimulatedServices(injector)
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    factory = AgentFactory(store, engine, services, PolicyEngine())

    await engine.create_workflow(
        name="Worker interruption reconciliation",
        scenario="worker_interruption",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=create_acme_contract(workflow_id),
        workflow_id=workflow_id,
    )
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    idempotency_key = "idem-worker-interruption-billing"
    external_result = await services.setup_billing(
        workflow_id=workflow_id,
        customer_id="acme-001",
        provider="paypal",
        plan_tier="enterprise",
        billing_cycle="monthly",
        idempotency_key=idempotency_key,
    )
    await engine.record_step_started(
        workflow_id,
        {
            "step_id": "step-worker-interruption",
            "workflow_id": workflow_id,
            "name": "Configure Billing (paypal)",
            "tool_name": "setup_billing",
            "tool_args": {
                "customer_id": "acme-001",
                "provider": "paypal",
                "plan_tier": "enterprise",
                "billing_cycle": "monthly",
            },
            "idempotency_key": idempotency_key,
            "status": "PENDING",
        },
    )

    async def interrupted_runner(*args, **kwargs):
        raise CrashBeforePersistenceError("post-write interruption")
        yield  # pragma: no cover - keeps this an async generator for ADK Runner

    monkeypatch.setattr(
        "google.adk.runners.Runner.run_async", interrupted_runner
    )

    result = await run_workflow_agent(workflow_id, store, engine, factory)
    workflow = await store.get_workflow(workflow_id)
    step = await store.get_step(workflow_id, "step-worker-interruption")

    assert result["status"] == "RECOVERING"
    assert result["needs_redispatch"] is True
    assert workflow["state"] == WorkflowState.RECOVERING.value
    assert step["status"] == "COMPLETED"
    assert step["result"]["subscription_id"] == external_result["subscription_id"]
    assert len(services._billing_records) == 1


def test_recovery_proof_requires_completion_and_verified_contract_data():
    """The proof surface reads verification evidence rather than scenario copy."""
    html = (ROOT / "backend/api/static/index.html").read_text()
    app_js = ROOT / "backend/api/static/app.js"
    js = app_js.read_text()

    assert "if (wf.state !== 'COMPLETED') return;" in js
    assert "allOutcomesVerified" in js
    assert "item?.evidence_type === 'VERIFICATION'" in js
    for element_id in (
        "proof-workflow-id",
        "proof-action-result",
        "proof-evidence-ids",
        "proof-final-state",
        "proof-timestamp",
    ):
        assert f'id="{element_id}"' in html

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable; static syntax check skipped")
    result = subprocess.run(
        [node, "--check", str(app_js)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_judge_materials_label_recovery_proof_truthfully():
    """Submission materials distinguish RecoveryOS controls from unintegrated GEAP."""
    readme = (ROOT / "README.md").read_text().lower()
    submission = (ROOT / "docs/HACKATHON_SUBMISSION.md").read_text().lower()

    assert "evidence-backed recovery proof" in readme
    assert "not cryptographically signed" in readme
    assert "tamper-evident" in readme
    assert "gemini enterprise agent platform" in readme
    assert "gemini 3.5 flash" in submission
    assert "gemini enterprise agent platform" in submission
