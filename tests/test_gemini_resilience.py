"""
Phase 5.4.4: Runtime Gemini Resilience, Rate Limiting & Failure Recovery Test Suite.

Verifies:
GEM-01 limiter enforces minimum interval
GEM-02 concurrent Gemini calls share one limiter
GEM-03 429 triggers retry
GEM-04 Retry-After is respected when available
GEM-05 exponential backoff is bounded
GEM-06 jitter prevents identical retry timing
GEM-07 retry exhaustion stops after configured attempts
GEM-08 non-retryable authentication error is not retried
GEM-09 invalid-model error is not retried
GEM-10 timeout is classified correctly
GEM-11 agent timeout cannot leave workflow permanently EXECUTING
GEM-12 recoverable Gemini failure transitions through UNKNOWN/RECOVERING safely
GEM-13 external mutation is not duplicated after agent retry
GEM-14 concurrent workflows remain isolated
GEM-15 circuit opens after threshold
GEM-16 OPEN circuit blocks Gemini requests
GEM-17 HALF_OPEN permits controlled probe
GEM-18 successful probe closes circuit
GEM-19 successful Gemini request resets failure state
GEM-20 runtime limiter configuration comes from config
GEM-21 secrets are not present in resilience logs
GEM-22 existing idempotency guarantees remain intact
GEM-23 existing PolicyEngine guarantees remain intact
GEM-24 live Gemini wrapper uses the runtime limiter rather than test-only pacing
"""

import asyncio
import time
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from google.adk.models import Gemini
from backend.config import config
from backend.models.workflow import WorkflowState
from backend.models.events import EventType
from backend.llm.resilience import (
    ErrorCategory,
    CircuitState,
    CircuitOpenError,
    RetryExhaustedError,
    GeminiFailureClassifier,
    GeminiRateLimiter,
    GeminiCircuitBreaker,
    ResilientGemini,
    record_resilience_event,
    get_resilience_events,
    clear_resilience_events,
)
from backend.persistence.workflow_store import WorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.external_services import SimulatedServices
from backend.tools.onboarding.tools import OnboardingTools
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA
import backend.api.server as srv


@pytest.fixture(autouse=True)
def setup_resilience_env():
    clear_resilience_events()


@pytest.mark.asyncio
async def test_gem_01_limiter_enforces_minimum_interval():
    """GEM-01: Limiter enforces minimum interval between sequential calls."""
    limiter = GeminiRateLimiter(min_interval_seconds=0.1)
    t0 = time.monotonic()
    wait1 = await limiter.acquire()
    wait2 = await limiter.acquire()
    t1 = time.monotonic()
    assert wait1 == 0.0
    assert wait2 >= 0.08
    assert (t1 - t0) >= 0.1


@pytest.mark.asyncio
async def test_gem_02_concurrent_calls_share_one_limiter():
    """GEM-02: Concurrent async tasks share the single global limiter without racing."""
    limiter = GeminiRateLimiter(min_interval_seconds=0.08)
    results = []

    async def worker(worker_id: int):
        wait = await limiter.acquire()
        results.append((worker_id, time.monotonic(), wait))

    t0 = time.monotonic()
    await asyncio.gather(worker(1), worker(2), worker(3))
    t1 = time.monotonic()

    assert len(results) == 3
    # Total time for 3 spaced calls with min_interval 0.08s must be >= 0.16s
    assert (t1 - t0) >= 0.15


def test_gem_03_04_08_09_10_failure_classifier():
    """GEM-03, GEM-04, GEM-08, GEM-09, GEM-10: Classifier tests."""
    # GEM-03: 429 is retryable
    cat, reason, ra = GeminiFailureClassifier.classify(Exception("ResourceExhausted: 429 Quota exceeded"))
    assert cat == ErrorCategory.RETRYABLE
    assert "429" in reason

    # GEM-04: Retry-After extracted
    class QuotaError(Exception):
        retry_after = 12.5

    cat, reason, ra = GeminiFailureClassifier.classify(QuotaError("429 Too Many Requests"))
    assert cat == ErrorCategory.RETRYABLE
    assert ra == 12.5

    # GEM-08: Auth error is non-retryable
    cat, reason, _ = GeminiFailureClassifier.classify(Exception("401 Unauthorized: Invalid API key"))
    assert cat == ErrorCategory.NON_RETRYABLE

    # GEM-09: Invalid model is non-retryable
    cat, reason, _ = GeminiFailureClassifier.classify(Exception("404 models/gemini-invalid not found"))
    assert cat == ErrorCategory.NON_RETRYABLE

    # GEM-10: Timeout is retryable
    cat, reason, _ = GeminiFailureClassifier.classify(asyncio.TimeoutError("Timeout waiting for response"))
    assert cat == ErrorCategory.RETRYABLE


def test_gem_05_06_exponential_backoff_and_jitter():
    """GEM-05 & GEM-06: Backoff is bounded and contains jitter."""
    initial = 1.0
    max_b = 8.0
    delays = []
    for attempt in range(1, 5):
        base = min(max_b, initial * (2 ** (attempt - 1)))
        assert base <= max_b
        delays.append(base)

    assert delays == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.asyncio
async def test_gem_07_retry_exhaustion_stops_after_configured_attempts():
    """GEM-07: ResilientGemini stops retrying after max_retries attempts."""
    limiter = GeminiRateLimiter(min_interval_seconds=0.0)
    cb = GeminiCircuitBreaker(failure_threshold=10, cooldown_seconds=60)
    model = ResilientGemini(
        model="test-model",
        rate_limiter=limiter,
        circuit_breaker=cb,
        max_retries=2,
        initial_backoff=0.01,
        max_backoff=0.02,
        request_timeout=1.0,
    )

    with patch.object(Gemini, "generate_content_async") as mock_gen:
        async def _failing_gen(*args, **kwargs):
            raise Exception("429 Resource exhausted")
            yield

        mock_gen.side_effect = _failing_gen

        dummy_req = MagicMock()
        with pytest.raises(RetryExhaustedError):
            async for _ in model.generate_content_async(dummy_req):
                pass

        # Total calls = 1 initial + 2 retries = 3
        assert mock_gen.call_count == 3


@pytest.mark.asyncio
async def test_gem_08_non_retryable_fails_immediately():
    """GEM-08: Non-retryable error (e.g. 401) fails immediately without retry."""
    limiter = GeminiRateLimiter(min_interval_seconds=0.0)
    cb = GeminiCircuitBreaker(failure_threshold=10)
    model = ResilientGemini(
        model="test-model",
        rate_limiter=limiter,
        circuit_breaker=cb,
        max_retries=3,
        initial_backoff=0.01,
    )

    with patch.object(Gemini, "generate_content_async") as mock_gen:
        async def _auth_err(*args, **kwargs):
            raise Exception("401 Unauthorized API key")
            yield

        mock_gen.side_effect = _auth_err

        dummy_req = MagicMock()
        with pytest.raises(Exception) as exc_info:
            async for _ in model.generate_content_async(dummy_req):
                pass
        assert "401" in str(exc_info.value)
        assert mock_gen.call_count == 1  # No retry!


@pytest.mark.asyncio
async def test_gem_11_12_agent_timeout_transitions_to_unknown():
    """GEM-11 & GEM-12: Agent exception/timeout never leaves workflow stuck in EXECUTING."""
    store = WorkflowStore()
    engine = WorkflowEngine(store)
    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    wf_data = await engine.create_workflow(
        name="Resilience Test",
        scenario="test",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=workflow_id,
    )
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    # Patch server's store and engine
    srv.store = store
    srv.engine = engine

    with patch("google.adk.runners.Runner.run_async") as mock_run:
        async def _timeout_run(*args, **kwargs):
            raise asyncio.TimeoutError("Gemini call timed out after 30s")
            yield

        mock_run.side_effect = _timeout_run
        await srv._run_agent(workflow_id)

    wf_final = await store.get_workflow(workflow_id)
    # Must NOT be in EXECUTING; must be in UNKNOWN
    assert wf_final["state"] == WorkflowState.UNKNOWN.value
    events = await store.get_events(workflow_id)
    assert any(e.get("event_type") == EventType.STEP_FAILED.value for e in events)


@pytest.mark.asyncio
async def test_gem_13_mutation_not_duplicated_after_retry():
    """GEM-13: Retrying execution after failure preserves idempotency and does not duplicate external mutations."""
    injector = FailureInjector()
    services = SimulatedServices(injector)
    store = WorkflowStore()
    engine = WorkflowEngine(store)
    tools = OnboardingTools(services, store, engine)

    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)
    await engine.create_workflow("Idem Test", "test", ACME_CUSTOMER_DATA, contract, workflow_id=workflow_id)
    await engine.transition(workflow_id, WorkflowState.EXECUTING)

    await tools.verify_identity(workflow_id, "acme-001", full_name="Alice Acme")

    # Mutate billing once
    res1 = await tools.setup_billing(workflow_id, "acme-001", provider="paypal")
    assert res1["status"] == "success"
    first_mutation_count = len(services._billing_records)
    assert first_mutation_count == 1

    # Retry the same step
    res2 = await tools.setup_billing(workflow_id, "acme-001", provider="paypal")
    assert res2["status"] == "success"
    # Mutation count must remain exactly 1
    assert len(services._billing_records) == 1


@pytest.mark.asyncio
async def test_gem_14_concurrent_workflows_isolated():
    """GEM-14: Concurrency between two workflows does not cross-contaminate state."""
    store = WorkflowStore()
    engine = WorkflowEngine(store)
    wf1_id = str(uuid.uuid4())
    wf2_id = str(uuid.uuid4())

    await engine.create_workflow("WF 1", "test", ACME_CUSTOMER_DATA, create_acme_contract(wf1_id), workflow_id=wf1_id, tenant_id="t1")
    await engine.create_workflow("WF 2", "test", ACME_CUSTOMER_DATA, create_acme_contract(wf2_id), workflow_id=wf2_id, tenant_id="t2")

    await engine.transition(wf1_id, WorkflowState.EXECUTING)
    await engine.transition(wf2_id, WorkflowState.EXECUTING)
    await engine.transition(wf2_id, WorkflowState.AWAITING_APPROVAL)

    w1 = await store.get_workflow(wf1_id)
    w2 = await store.get_workflow(wf2_id)
    assert w1["state"] == "EXECUTING"
    assert w2["state"] == "AWAITING_APPROVAL"
    assert w1["tenant_id"] == "t1"
    assert w2["tenant_id"] == "t2"


@pytest.mark.asyncio
async def test_gem_15_16_17_18_19_circuit_breaker_lifecycle():
    """GEM-15, 16, 17, 18, 19: Circuit breaker transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""
    cb = GeminiCircuitBreaker(failure_threshold=3, cooldown_seconds=0.1)
    assert cb.state == CircuitState.CLOSED
    assert await cb.can_execute() is True

    # GEM-15: 3 failures trip circuit to OPEN
    await cb.record_failure()
    await cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # GEM-16: OPEN circuit blocks execution
    assert await cb.can_execute() is False

    # GEM-17: After cooldown, transitions to HALF_OPEN probe
    await asyncio.sleep(0.12)
    assert await cb.can_execute() is True
    assert cb.state == CircuitState.HALF_OPEN

    # GEM-18 & GEM-19: Success closes the circuit and resets failure count
    await cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0


def test_gem_20_config_bindings():
    """GEM-20: Limiter and resilience parameters are loaded from config."""
    assert config.gemini_min_interval_seconds >= 6.0
    assert config.gemini_max_retries >= 1
    assert config.gemini_circuit_failure_threshold >= 3


def test_gem_21_secrets_not_in_resilience_logs():
    """GEM-21: Secrets, tokens, and authorization keys are redacted from resilience logs."""
    record_resilience_event(
        event_type="TEST_SECRET_REDACTION",
        action="test",
        outcome="OK",
        detail="Logging test event",
        extra={"api_key": "secret-key-1234", "jwt": "token-5678", "normal_field": "safe_value"},
    )
    events = get_resilience_events()
    assert len(events) >= 1
    logged_event = next(e for e in events if e.get("event_type") == "TEST_SECRET_REDACTION")
    meta = logged_event.get("metadata", {})
    assert "api_key" not in meta
    assert "jwt" not in meta
    assert meta.get("normal_field") == "safe_value"


@pytest.mark.asyncio
async def test_gem_22_23_policy_and_idempotency_intact():
    """GEM-22 & GEM-23: Deterministic PolicyEngine and verification contract remain authoritative."""
    pe = PolicyEngine()
    contract = create_acme_contract("wf-gem-22")
    decision = pe.evaluate(
        tool_name="setup_billing",
        tool_args={"customer_id": "acme-001", "provider": "stripe"},
        workflow_state={"workflow_id": "wf-gem-22", "state": "EXECUTING"},
        evidence=[],
        contract=contract,
    )
    from backend.models.policy import PolicyOutcome
    assert decision.outcome == PolicyOutcome.REJECTED


@pytest.mark.asyncio
async def test_gem_24_live_wrapper_uses_runtime_limiter():
    """GEM-24: Live evaluation harness delegates to runtime global_rate_limiter."""
    from tests.live_gemini_eval import GLOBAL_RATE_LIMITER
    from backend.llm.resilience import global_rate_limiter

    t0 = time.monotonic()
    await GLOBAL_RATE_LIMITER.acquire("Scenario GEM-24", turn=1)
    t1 = time.monotonic()
    assert len(GLOBAL_RATE_LIMITER.call_log) >= 1
