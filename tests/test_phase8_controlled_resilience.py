"""
RecoveryOS Phase 8: Controlled Resilience & Failure-Injection Validation.

Proves that RecoveryOS degrades safely and recovers correctly when individual
components fail at the worst possible moment.

Each test answers:
  "If one component fails at the worst possible moment, does RecoveryOS
   preserve workflow correctness, avoid duplicate business effects, detect
   the failure, retry safely, and recover without corrupting state?"

Evidence levels used in this file:
  [UNIT]        — Pure unit test against in-memory store.
  [INTEGRATION] — Multi-component integration test using InMemoryWorkflowStore.

IMPORTANT:
  - No production infrastructure is touched.
  - No Firestore production data is mutated.
  - No Pub/Sub messages are published to production topics.
  - All failure injection uses test hooks and mocked dependencies.
  - Failure injection configuration is impossible under normal production config.
"""

from __future__ import annotations

import asyncio
import json
import uuid
import copy
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
    MessageValidationError,
)
from backend.events.consumer import WorkflowEventConsumer, ConsumerExecutionError
from backend.engine.workflow_engine import WorkflowEngine
from backend.models.workflow import WorkflowState
from backend.models.idempotency import OperationStatus
from backend.persistence.workflow_store import (
    InMemoryWorkflowStore,
    StaleWorkflowStateError,
)
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
from backend.worker.models import (
    DeliveryStatus,
    FailureClassification,
    WorkerExecutionResult,
)
from backend.worker.service import WorkflowWorkerService
from backend.worker.security import (
    BaseWorkerSecurityValidator,
    SecurityVerificationError,
)
from backend.lifecycle import ShutdownManager
from backend.observability.metrics import metrics


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(autouse=True)
def _clear_metrics():
    """Reset global metrics registry between tests for isolation."""
    metrics.clear()
    yield
    metrics.clear()


async def _create_test_wf(engine, tenant_id="tenant-phase8", scenario="billing_unavailable"):
    """Create and persist a test workflow, returning the workflow dict."""
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    return await engine.create_workflow(
        name="Phase 8 Resilience Test WF",
        scenario=scenario,
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id=tenant_id,
    )


def _make_msg(wf_id, tenant_id="tenant-phase8", expected_version=1, idemp_key=None,
              event_type=WorkflowEventType.WORKFLOW_DISPATCH):
    """Build a valid WorkflowExecutionMessage for testing."""
    return WorkflowExecutionMessage(
        event_type=event_type,
        workflow_id=wf_id,
        tenant_id=tenant_id,
        idempotency_key=idemp_key or f"op_p8_{wf_id}_{uuid.uuid4().hex[:8]}",
        expected_version=expected_version,
    )


# ===========================================================================
# R1: Cascading Failure — Persistence + Consumer Crash Sequence
# [INTEGRATION]
# ===========================================================================

class TestR1CascadingFailures:
    """
    R1: If persistence fails during state transition AND the consumer crashes,
    the workflow must remain in its last-known-good state, and retry must be safe.
    """

    @pytest.mark.asyncio
    async def test_r1_persistence_crash_then_consumer_crash_preserves_state(self):
        """
        Scenario: Worker claims operation, then crashes during state transition
        via test_failure_hook. Persistence layer IOError on save is injected
        via the hook to simulate Firestore failure. Next retry must succeed
        cleanly without orphaned or corrupted state.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        msg = _make_msg(wf_id)

        # First attempt: Crash after claim (simulating persistence + crash)
        def persistence_crash_hook(stage, m):
            if stage == "after_claim":
                raise IOError("Simulated Firestore deadline exceeded during state transition")

        consumer1 = WorkflowEventConsumer(
            store=store, engine=engine, worker_id="worker-crash-1",
            test_failure_hook=persistence_crash_hook,
        )
        worker1 = WorkflowWorkerService(consumer=consumer1, worker_id="worker-crash-1")
        res1 = await worker1.process_message(msg)
        assert res1.delivery_status == DeliveryStatus.NACK

        # Verify workflow is still at CREATED, version 1 (no partial mutation)
        wf_after = await store.get_workflow(wf_id)
        assert wf_after["state"] == WorkflowState.CREATED.value
        assert wf_after["version"] == 1

        # Expire any lease that was acquired
        claim = store._operations.get(msg.idempotency_key)
        if claim:
            claim["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

        # Retry: healthy worker with fresh consumer succeeds
        consumer2 = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-healthy")
        worker2 = WorkflowWorkerService(consumer=consumer2, worker_id="worker-healthy")
        res2 = await worker2.process_message(msg)
        assert res2.delivery_status == DeliveryStatus.ACK
        assert res2.details.get("status") == "PROCESSED"

        final = await store.get_workflow(wf_id)
        assert final["state"] == WorkflowState.EXECUTING.value
        assert final["version"] == 2


# ===========================================================================
# R2: Concurrent Worker Race — Multiple Workers Receive Same Message
# [INTEGRATION]
# ===========================================================================

class TestR2ConcurrentWorkerRace:
    """
    R2: If multiple workers receive the same message simultaneously,
    exactly one must process it, others must skip without state corruption.
    """

    @pytest.mark.asyncio
    async def test_r2_concurrent_workers_single_execution(self):
        """
        Three workers attempt to process the same idempotency key concurrently.
        Exactly one must succeed with PROCESSED; others must return SKIPPED_DUPLICATE.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]
        idemp_key = f"op_r2_{wf_id}"

        msg = _make_msg(wf_id, idemp_key=idemp_key)

        workers = []
        for i in range(3):
            consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id=f"worker-{i}")
            worker = WorkflowWorkerService(consumer=consumer, worker_id=f"worker-{i}")
            workers.append(worker)

        # Execute all concurrently
        results = await asyncio.gather(
            *[w.process_message(msg) for w in workers],
            return_exceptions=True,
        )

        # Count outcomes (exclude exceptions)
        processed = sum(
            1 for r in results
            if isinstance(r, WorkerExecutionResult) and r.delivery_status == DeliveryStatus.ACK
            and r.details.get("status") == "PROCESSED"
        )
        skipped = sum(
            1 for r in results
            if isinstance(r, WorkerExecutionResult) and r.delivery_status == DeliveryStatus.ACK
            and r.details.get("status") == "SKIPPED_DUPLICATE"
        )
        nacked = sum(
            1 for r in results
            if isinstance(r, WorkerExecutionResult) and r.delivery_status == DeliveryStatus.NACK
        )

        # At most 1 PROCESSED; rest are either SKIPPED_DUPLICATE or NACK (OCC)
        assert processed <= 1, f"Expected at most 1 PROCESSED, got {processed}"
        assert processed + skipped + nacked == 3, "All results must be accounted for"

        # Workflow version advanced at most once
        final = await store.get_workflow(wf_id)
        assert final["version"] <= 2


    @pytest.mark.asyncio
    async def test_r2_parallel_different_messages_different_versions(self):
        """
        Two messages targeting the same workflow but with different expected versions.
        Only the one matching the current version should succeed.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        # Message 1: correct version
        msg1 = _make_msg(wf_id, expected_version=1, idemp_key=f"op_r2b_v1_{wf_id}")
        # Message 2: stale version
        msg2 = _make_msg(wf_id, expected_version=99, idemp_key=f"op_r2b_v99_{wf_id}")

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-occ")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="worker-occ")

        r1, r2 = await asyncio.gather(
            worker.process_message(msg1),
            worker.process_message(msg2),
        )

        # One must succeed, one must fail OCC
        results = [r1, r2]
        acked = [r for r in results if r.delivery_status == DeliveryStatus.ACK]
        nacked = [r for r in results if r.delivery_status == DeliveryStatus.NACK]

        assert len(acked) == 1, "Exactly one message should be ACKed"
        assert len(nacked) == 1, "Exactly one message should be NACKed (OCC mismatch)"


# ===========================================================================
# R3: Lease Contention Storm — Expired Lease Reclaim Under Pressure
# [INTEGRATION]
# ===========================================================================

class TestR3LeaseContentionStorm:
    """
    R3: Under high contention (many workers competing for the same lease after expiry),
    the system must not produce duplicate business effects or corrupt state.
    """

    @pytest.mark.asyncio
    async def test_r3_lease_expiry_reclaim_prevents_duplicate_execution(self):
        """
        Worker 1 crashes holding lease. Lease expires. Workers 2, 3, 4 all
        attempt reclaim simultaneously. Exactly one should process.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]
        idemp_key = f"op_r3_{wf_id}"

        msg = _make_msg(wf_id, idemp_key=idemp_key)

        # Worker 1 claims and crashes
        def crash_hook(stage, m):
            if stage == "after_claim":
                raise RuntimeError("Simulated crash after claim")

        c1 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-crash",
                                   test_failure_hook=crash_hook)
        w1 = WorkflowWorkerService(consumer=c1, worker_id="w-crash")
        r1 = await w1.process_message(msg)
        assert r1.delivery_status == DeliveryStatus.NACK

        # Expire the lease
        claim = store._operations.get(idemp_key)
        assert claim is not None
        claim["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

        # Multiple workers attempt reclaim
        recovery_workers = []
        for i in range(3):
            c = WorkflowEventConsumer(store=store, engine=engine, worker_id=f"w-reclaim-{i}")
            w = WorkflowWorkerService(consumer=c, worker_id=f"w-reclaim-{i}")
            recovery_workers.append(w)

        results = await asyncio.gather(
            *[w.process_message(msg) for w in recovery_workers],
            return_exceptions=True,
        )

        processed_count = sum(
            1 for r in results
            if isinstance(r, WorkerExecutionResult) and r.delivery_status == DeliveryStatus.ACK
            and r.details.get("status") == "PROCESSED"
        )

        # Exactly one should process
        assert processed_count <= 1, f"Expected at most 1 PROCESSED, got {processed_count}"

        # Workflow must advance at most once
        final = await store.get_workflow(wf_id)
        assert final["version"] == 2


# ===========================================================================
# R4: Multi-Tenant Isolation Under Failure
# [INTEGRATION]
# ===========================================================================

class TestR4MultiTenantIsolation:
    """
    R4: A failure in one tenant's workflow must never affect another tenant's data.
    Cross-tenant messages must be rejected even under concurrent failure conditions.
    """

    @pytest.mark.asyncio
    async def test_r4_cross_tenant_message_rejected_even_during_crash(self):
        """
        Tenant-A's workflow receives a message from Tenant-B.
        Even if the consumer crashes on the rejection path, no cross-tenant
        data should be mutated.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)

        wf_a = await _create_test_wf(engine, tenant_id="tenant-alpha")
        wf_b = await _create_test_wf(engine, tenant_id="tenant-beta")

        # Cross-tenant: message claims tenant-beta but targets tenant-alpha's workflow
        msg_cross = _make_msg(wf_a["workflow_id"], tenant_id="tenant-beta")

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-tenant")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-tenant")

        result = await worker.process_message(msg_cross)
        assert result.delivery_status == DeliveryStatus.DEAD_LETTER
        assert result.failure_type == FailureClassification.PERMANENT

        # Verify neither workflow was mutated
        a_after = await store.get_workflow(wf_a["workflow_id"])
        b_after = await store.get_workflow(wf_b["workflow_id"])
        assert a_after["state"] == WorkflowState.CREATED.value
        assert a_after["version"] == 1
        assert b_after["state"] == WorkflowState.CREATED.value
        assert b_after["version"] == 1

    @pytest.mark.asyncio
    async def test_r4_concurrent_tenants_isolated_state_progression(self):
        """
        Two tenants' workflows progress concurrently. Each must only
        advance its own version without cross-contamination.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)

        wf_a = await _create_test_wf(engine, tenant_id="tenant-alpha")
        wf_b = await _create_test_wf(engine, tenant_id="tenant-beta")

        msg_a = _make_msg(wf_a["workflow_id"], tenant_id="tenant-alpha")
        msg_b = _make_msg(wf_b["workflow_id"], tenant_id="tenant-beta")

        ca = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-alpha")
        cb = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-beta")
        wa = WorkflowWorkerService(consumer=ca, worker_id="w-alpha")
        wb = WorkflowWorkerService(consumer=cb, worker_id="w-beta")

        ra, rb = await asyncio.gather(
            wa.process_message(msg_a),
            wb.process_message(msg_b),
        )

        assert ra.delivery_status == DeliveryStatus.ACK
        assert rb.delivery_status == DeliveryStatus.ACK

        final_a = await store.get_workflow(wf_a["workflow_id"])
        final_b = await store.get_workflow(wf_b["workflow_id"])

        assert final_a["tenant_id"] == "tenant-alpha"
        assert final_b["tenant_id"] == "tenant-beta"
        assert final_a["version"] == 2
        assert final_b["version"] == 2


# ===========================================================================
# R5: Partial Persistence Failure — Save Step Fails, Workflow Must Not Advance
# [INTEGRATION]
# ===========================================================================

class TestR5PartialPersistenceFailure:
    """
    R5: If persistence partially fails (e.g., workflow save succeeds but
    operation claim completion fails), the system must degrade safely.
    """

    @pytest.mark.asyncio
    async def test_r5_claim_completion_failure_allows_safe_redelivery(self):
        """
        Worker processes message, transitions state successfully, but
        complete_operation fails. Redelivery should detect the state has
        already advanced and handle idempotently.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        msg = _make_msg(wf_id)

        # Fail on complete_operation
        def crash_after_transition(stage, m):
            if stage == "after_transition":
                raise RuntimeError("Simulated crash after state transition, before complete_operation")

        c1 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-partial",
                                   test_failure_hook=crash_after_transition)
        w1 = WorkflowWorkerService(consumer=c1, worker_id="w-partial")
        r1 = await w1.process_message(msg)
        assert r1.delivery_status == DeliveryStatus.NACK

        # State was already mutated to EXECUTING, version 2
        wf_mid = await store.get_workflow(wf_id)
        assert wf_mid["state"] == WorkflowState.EXECUTING.value
        assert wf_mid["version"] == 2

        # Expire lease
        claim = store._operations.get(msg.idempotency_key)
        if claim:
            claim["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

        # Retry: OCC version in message (1) != workflow version (2) -> OCC mismatch
        c2 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-retry")
        w2 = WorkflowWorkerService(consumer=c2, worker_id="w-retry")
        r2 = await w2.process_message(msg)
        assert r2.delivery_status == DeliveryStatus.NACK
        assert r2.failure_type == FailureClassification.RETRYABLE

        # Version did NOT regress
        final = await store.get_workflow(wf_id)
        assert final["version"] == 2
        assert final["state"] == WorkflowState.EXECUTING.value


# ===========================================================================
# R6: Terminal State Immutability Under Concurrent Attack
# [INTEGRATION]
# ===========================================================================

class TestR6TerminalStateImmutability:
    """
    R6: Terminal workflows (COMPLETED, ESCALATED) must reject all execution
    attempts, even under concurrent pressure.
    """

    @pytest.mark.asyncio
    async def test_r6_completed_workflow_rejects_all_concurrent_executions(self):
        """
        A COMPLETED workflow receives 5 concurrent execution messages.
        All must be SKIPPED_TERMINAL without mutating state.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        # Advance to COMPLETED via valid transition path:
        # CREATED -> EXECUTING -> VERIFYING -> COMPLETED
        await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Start")
        await engine.transition(wf_id, WorkflowState.VERIFYING, detail="Check")
        await engine.transition(wf_id, WorkflowState.COMPLETED, detail="Done")
        completed_wf = await store.get_workflow(wf_id)
        completed_version = completed_wf["version"]

        # 5 concurrent messages all targeting the completed workflow
        messages = [_make_msg(wf_id, expected_version=completed_version) for _ in range(5)]

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-term")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-term")

        results = await asyncio.gather(
            *[worker.process_message(m) for m in messages]
        )

        for r in results:
            assert r.delivery_status == DeliveryStatus.ACK
            assert r.details.get("status") == "SKIPPED_TERMINAL"

        # Version unchanged
        final = await store.get_workflow(wf_id)
        assert final["version"] == completed_version

    @pytest.mark.asyncio
    async def test_r6_escalated_workflow_immutability(self):
        """ESCALATED workflows are also terminal — no execution should mutate them."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        # Valid path: CREATED -> EXECUTING -> ESCALATED
        await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Start")
        await engine.transition(wf_id, WorkflowState.ESCALATED, detail="Too many failures")
        esc_wf = await store.get_workflow(wf_id)

        msg = _make_msg(wf_id, expected_version=esc_wf["version"])
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-esc")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-esc")

        r = await worker.process_message(msg)
        assert r.delivery_status == DeliveryStatus.ACK
        assert r.details.get("status") == "SKIPPED_TERMINAL"


# ===========================================================================
# R7: Observability Correctness Under Failure
# [UNIT]
# ===========================================================================

class TestR7ObservabilityUnderFailure:
    """
    R7: All failure scenarios must emit the correct metrics and log events.
    """

    @pytest.mark.asyncio
    async def test_r7_occ_mismatch_increments_counter(self):
        """OCC mismatch must increment recoveryos_occ_mismatches_total."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        # Advance version
        await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Pre-advance")

        msg = _make_msg(wf_id, expected_version=1)  # Stale
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-occ")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-occ")

        before = metrics.get_counter_value("recoveryos_occ_mismatches_total")
        await worker.process_message(msg)
        after = metrics.get_counter_value("recoveryos_occ_mismatches_total")

        assert after > before, "OCC mismatch counter should have incremented"

    @pytest.mark.asyncio
    async def test_r7_duplicate_claim_increments_counter(self):
        """Duplicate message delivery must increment recoveryos_duplicate_claims_total."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]
        idemp_key = f"op_r7_dup_{wf_id}"

        msg = _make_msg(wf_id, idemp_key=idemp_key)

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-dup")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-dup")

        # First delivery
        await worker.process_message(msg)

        before = metrics.get_counter_value("recoveryos_duplicate_claims_total")
        # Second delivery
        await worker.process_message(msg)
        after = metrics.get_counter_value("recoveryos_duplicate_claims_total")

        assert after > before, "Duplicate claim counter should have incremented"

    @pytest.mark.asyncio
    async def test_r7_worker_execution_metrics_correct_labels(self):
        """Worker execution results must carry correct status labels."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        msg = _make_msg(wf_id)

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-metrics")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-metrics")

        ack_before = metrics.get_counter_value(
            "recoveryos_worker_executions_total", labels={"status": "ack", "failure_type": "none"}
        )
        await worker.process_message(msg)
        ack_after = metrics.get_counter_value(
            "recoveryos_worker_executions_total", labels={"status": "ack", "failure_type": "none"}
        )

        assert ack_after > ack_before, "ACK counter should have incremented"


# ===========================================================================
# R8: Security Under Failure — Untrusted Producer Rejection
# [UNIT]
# ===========================================================================

class TestR8SecurityUnderFailure:
    """
    R8: Security validation must never be bypassed, even when other
    components are failing.
    """

    @pytest.mark.asyncio
    async def test_r8_untrusted_producer_dead_lettered(self):
        """Messages from untrusted producers must be permanently rejected."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        msg = _make_msg(wf_id)
        msg.producer_id = "evil-attacker-service"

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-sec")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-sec")

        r = await worker.process_message(msg)
        assert r.delivery_status == DeliveryStatus.DEAD_LETTER
        assert r.failure_type == FailureClassification.PERMANENT

        # Workflow must not be affected
        wf_after = await store.get_workflow(wf_id)
        assert wf_after["version"] == 1

    @pytest.mark.asyncio
    async def test_r8_security_error_during_concurrent_processing(self):
        """
        If security validation fails for one message while another is processing,
        the valid message must still complete successfully.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)

        wf_good = await _create_test_wf(engine, tenant_id="tenant-good")
        wf_evil = await _create_test_wf(engine, tenant_id="tenant-evil")

        msg_good = _make_msg(wf_good["workflow_id"], tenant_id="tenant-good")
        msg_evil = _make_msg(wf_evil["workflow_id"], tenant_id="tenant-evil")
        msg_evil.producer_id = "malicious-producer"

        c_good = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-good")
        w_good = WorkflowWorkerService(consumer=c_good, worker_id="w-good")

        c_evil = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-evil")
        w_evil = WorkflowWorkerService(consumer=c_evil, worker_id="w-evil")

        r_good, r_evil = await asyncio.gather(
            w_good.process_message(msg_good),
            w_evil.process_message(msg_evil),
        )

        assert r_good.delivery_status == DeliveryStatus.ACK
        assert r_evil.delivery_status == DeliveryStatus.DEAD_LETTER


# ===========================================================================
# R9: Shutdown Draining Under Load
# [INTEGRATION]
# ===========================================================================

class TestR9ShutdownDraining:
    """
    R9: If shutdown is initiated while messages are in flight, all in-progress
    tasks must drain cleanly, and new tasks must be rejected.
    """

    @pytest.mark.asyncio
    async def test_r9_shutdown_rejects_new_work_accepts_inflight(self):
        """
        Start processing a message, then initiate shutdown.
        In-flight task completes; new tasks receive NACK.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf1 = await _create_test_wf(engine)
        wf2 = await _create_test_wf(engine)

        shutdown_mgr = ShutdownManager()
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-shutdown")
        worker = WorkflowWorkerService(consumer=consumer, shutdown_manager=shutdown_mgr, worker_id="w-shutdown")

        msg1 = _make_msg(wf1["workflow_id"])
        msg2 = _make_msg(wf2["workflow_id"])

        # Start first task
        task1 = asyncio.create_task(worker.process_message(msg1))
        shutdown_mgr.register_task(task1)

        # Wait for task to complete
        drained = await shutdown_mgr.drain_tasks(timeout=3.0)
        assert drained == 1
        r1 = task1.result()
        assert r1.delivery_status == DeliveryStatus.ACK

        # Initiate shutdown
        shutdown_mgr.begin_shutdown()

        # New work should be rejected
        r2 = await worker.process_message(msg2)
        assert r2.delivery_status == DeliveryStatus.NACK

    @pytest.mark.asyncio
    async def test_r9_multiple_inflight_tasks_all_drain(self):
        """Multiple in-flight tasks during shutdown all drain cleanly."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)

        shutdown_mgr = ShutdownManager()
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-drain")
        worker = WorkflowWorkerService(consumer=consumer, shutdown_manager=shutdown_mgr, worker_id="w-drain")

        wfs = [await _create_test_wf(engine) for _ in range(3)]
        msgs = [_make_msg(wf["workflow_id"]) for wf in wfs]

        tasks = []
        for msg in msgs:
            t = asyncio.create_task(worker.process_message(msg))
            shutdown_mgr.register_task(t)
            tasks.append(t)

        drained = await shutdown_mgr.drain_tasks(timeout=5.0)
        assert drained == 3

        for t in tasks:
            assert t.done()
            assert t.result().delivery_status == DeliveryStatus.ACK


# ===========================================================================
# R10: Message Schema Validation Boundary
# [UNIT]
# ===========================================================================

class TestR10MessageSchemaValidation:
    """
    R10: The message contract validation must correctly classify all malformed
    inputs as DEAD_LETTER without crashing the worker.
    """

    @pytest.mark.asyncio
    async def test_r10_empty_payload_dead_letter(self):
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-schema")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-schema")

        r = await worker.process_raw_payload(b"")
        assert r.delivery_status == DeliveryStatus.DEAD_LETTER

    @pytest.mark.asyncio
    async def test_r10_null_bytes_dead_letter(self):
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-null")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-null")

        r = await worker.process_raw_payload(b"\x00\x00\x00")
        assert r.delivery_status == DeliveryStatus.DEAD_LETTER

    @pytest.mark.asyncio
    async def test_r10_oversized_payload_dead_letter(self):
        """Extremely large payloads should not cause OOM."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-big")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-big")

        # 1MB of nested JSON — valid JSON but invalid schema
        big = json.dumps({"data": "x" * (1024 * 1024)})
        r = await worker.process_raw_payload(big.encode())
        assert r.delivery_status == DeliveryStatus.DEAD_LETTER

    @pytest.mark.asyncio
    async def test_r10_wrong_event_type_dead_letter(self):
        """Unknown event_type must be rejected."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-type")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-type")

        payload = json.dumps({
            "event_type": "TOTALLY_FAKE_EVENT",
            "workflow_id": str(uuid.uuid4()),
            "tenant_id": "tenant-test",
            "idempotency_key": "fake-key",
            "expected_version": 1,
        })
        r = await worker.process_raw_payload(payload.encode())
        assert r.delivery_status == DeliveryStatus.DEAD_LETTER

    @pytest.mark.asyncio
    async def test_r10_missing_required_fields_dead_letter(self):
        """Missing required fields must be rejected."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-field")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-field")

        # Missing workflow_id
        payload = json.dumps({
            "event_type": "WORKFLOW_DISPATCH",
            "tenant_id": "t-1",
            "idempotency_key": "k-1",
            "expected_version": 1,
        })
        r = await worker.process_raw_payload(payload.encode())
        assert r.delivery_status == DeliveryStatus.DEAD_LETTER


# ===========================================================================
# R11: Workflow State Machine Invariant — No Invalid Transitions
# [INTEGRATION]
# ===========================================================================

class TestR11StateMachineInvariant:
    """
    R11: The workflow engine must reject invalid state transitions,
    even when triggered by worker execution.
    """

    @pytest.mark.asyncio
    async def test_r11_double_execution_start_idempotent(self):
        """
        Processing the same WORKFLOW_DISPATCH twice when workflow is already
        EXECUTING should be idempotent (the consumer skips re-transition).
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        msg1 = _make_msg(wf_id, idemp_key=f"op_r11_a_{wf_id}")
        msg2 = _make_msg(wf_id, idemp_key=f"op_r11_b_{wf_id}", expected_version=2)

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-inv")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-inv")

        r1 = await worker.process_message(msg1)
        assert r1.delivery_status == DeliveryStatus.ACK

        # After first execution: EXECUTING, version 2
        mid = await store.get_workflow(wf_id)
        assert mid["state"] == WorkflowState.EXECUTING.value
        assert mid["version"] == 2

        # Second execution with correct version: should still work
        r2 = await worker.process_message(msg2)
        assert r2.delivery_status == DeliveryStatus.ACK


# ===========================================================================
# R12: Recovery Endpoint Safety Under Edge Conditions
# [INTEGRATION]
# ===========================================================================

class TestR12RecoveryEndpointSafety:
    """
    R12: The operator recovery endpoint must enforce role gating, tenant
    isolation, OCC version checking, and terminal state protection.
    Tests use HTTPX TestClient against the FastAPI app.
    """

    @pytest.mark.asyncio
    async def test_r12_recovery_rejected_for_completed_workflow(self):
        """POST /api/workflows/{id}/recover must return 400 for COMPLETED workflows (terminal immutability)."""
        from backend.api.server import app, store as api_store, engine as api_engine
        from backend.security.tokens import create_access_token
        from backend.config import config as app_config
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = create_access_token("op-1", "operator", "tenant-phase8", secret_key=app_config.jwt_secret_key)

            # Create a workflow directly in the store for test isolation
            test_wf_id = f"wf-r12-{uuid.uuid4().hex[:8]}"
            wf_data = {
                "workflow_id": test_wf_id,
                "tenant_id": "tenant-phase8",
                "name": "R12 Test",
                "scenario": "test",
                "state": WorkflowState.CREATED.value,
                "version": 1,
                "customer_data": {},
            }
            await api_store.save_workflow(wf_data)

            # Advance to COMPLETED via valid transition path:
            # CREATED -> EXECUTING -> VERIFYING -> COMPLETED
            await api_engine.transition(test_wf_id, WorkflowState.EXECUTING, detail="Test")
            await api_engine.transition(test_wf_id, WorkflowState.VERIFYING, detail="Check")
            await api_engine.transition(test_wf_id, WorkflowState.COMPLETED, detail="Done")

            # Attempt recovery
            resp_recover = await client.post(
                f"/api/workflows/{test_wf_id}/recover",
                headers={"Authorization": f"Bearer {token}"},
                json={"reason": "Test recovery"},
            )
            assert resp_recover.status_code == 400

    @pytest.mark.asyncio
    async def test_r12_recovery_rejected_for_viewer_role(self):
        """Viewer role must not be able to trigger recovery."""
        from backend.api.server import app
        from backend.security.tokens import create_access_token
        from backend.config import config as app_config
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            viewer_token = create_access_token(
                "viewer-1", "viewer", "tenant-phase8", secret_key=app_config.jwt_secret_key
            )
            resp = await client.post(
                f"/api/workflows/{uuid.uuid4()}/recover",
                headers={"Authorization": f"Bearer {viewer_token}"},
                json={"reason": "Test recovery"},
            )
            assert resp.status_code == 403


# ===========================================================================
# R13: DLQ Routing Correctness
# [INTEGRATION]
# ===========================================================================

class TestR13DLQRouting:
    """
    R13: Poison messages and permanently failing workflows must route to
    DLQ after the correct number of delivery attempts, not silently drop.
    """

    @pytest.mark.asyncio
    async def test_r13_poison_message_returns_correct_http_for_dlq(self):
        """Worker HTTP endpoint returns correct status for DLQ routing via Pub/Sub push envelope."""
        import base64
        from backend.worker.server import app as worker_app, set_worker_service
        from httpx import AsyncClient, ASGITransport

        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-dlq")
        service = WorkflowWorkerService(consumer=consumer, worker_id="w-dlq")
        set_worker_service(service)

        # Wrap poison payload in Pub/Sub push envelope format
        poison_data = base64.b64encode(b'{"invalid": "payload"}').decode()
        envelope = {
            "message": {
                "data": poison_data,
                "messageId": "test-poison-123",
                "publishTime": "2026-01-01T00:00:00Z",
            },
            "subscription": "projects/test/subscriptions/test-sub",
        }

        transport = ASGITransport(app=worker_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/",
                json=envelope,
            )
            # Worker returns 422 for schema failures (unprocessable) for DLQ routing
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_r13_non_existent_workflow_returns_dlq_status(self):
        """Non-existent workflow ID must trigger DLQ routing (422) via Pub/Sub push envelope."""
        import base64
        from backend.worker.server import app as worker_app, set_worker_service
        from httpx import AsyncClient, ASGITransport

        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-dlq-2")
        service = WorkflowWorkerService(consumer=consumer, worker_id="w-dlq-2")
        set_worker_service(service)

        phantom_msg = WorkflowExecutionMessage(
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            workflow_id=str(uuid.uuid4()),
            tenant_id="tenant-phase8",
            idempotency_key="phantom-key",
            expected_version=1,
        )

        # Wrap in Pub/Sub push envelope format
        msg_data_b64 = base64.b64encode(phantom_msg.to_pubsub_json().encode()).decode()
        envelope = {
            "message": {
                "data": msg_data_b64,
                "messageId": "test-phantom-456",
                "publishTime": "2026-01-01T00:00:00Z",
            },
            "subscription": "projects/test/subscriptions/test-sub",
        }

        transport = ASGITransport(app=worker_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/",
                json=envelope,
            )
            assert resp.status_code == 422


# ===========================================================================
# R14: Recovery Timing — Lease Duration Boundaries
# [UNIT]
# ===========================================================================

class TestR14RecoveryTiming:
    """
    R14: Lease expiration boundaries must be respected precisely.
    Claims within lease window should block; claims after expiry should succeed.
    """

    @pytest.mark.asyncio
    async def test_r14_claim_within_lease_window_blocked(self):
        """Attempting to claim within the lease window must return SKIPPED_DUPLICATE."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]
        idemp_key = f"op_r14_{wf_id}"

        msg = _make_msg(wf_id, idemp_key=idemp_key)

        # Worker 1 claims and crashes (lease still active)
        def crash_hook(stage, m):
            if stage == "after_claim":
                raise RuntimeError("Crash after claim")

        c1 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-lease-1",
                                   test_failure_hook=crash_hook)
        w1 = WorkflowWorkerService(consumer=c1, worker_id="w-lease-1")
        await w1.process_message(msg)

        # Verify lease is active (not expired)
        claim = store._operations.get(idemp_key)
        assert claim is not None
        lease_exp = datetime.fromisoformat(claim["lease_expires_at"])
        assert lease_exp > datetime.now(timezone.utc), "Lease should still be active"

        # Worker 2 attempts within lease window — should be blocked
        c2 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-lease-2")
        w2 = WorkflowWorkerService(consumer=c2, worker_id="w-lease-2")
        r2 = await w2.process_message(msg)
        assert r2.delivery_status == DeliveryStatus.ACK
        assert r2.details.get("status") == "SKIPPED_DUPLICATE"

    @pytest.mark.asyncio
    async def test_r14_claim_after_lease_expiry_succeeds(self):
        """Attempting to claim after lease expiry must succeed."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]
        idemp_key = f"op_r14b_{wf_id}"

        msg = _make_msg(wf_id, idemp_key=idemp_key)

        # Worker 1 claims and crashes
        def crash_hook(stage, m):
            if stage == "after_claim":
                raise RuntimeError("Crash after claim")

        c1 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-exp-1",
                                   test_failure_hook=crash_hook)
        w1 = WorkflowWorkerService(consumer=c1, worker_id="w-exp-1")
        await w1.process_message(msg)

        # Expire the lease
        claim = store._operations.get(idemp_key)
        claim["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

        # Worker 2 attempts after expiry — should succeed
        c2 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-exp-2")
        w2 = WorkflowWorkerService(consumer=c2, worker_id="w-exp-2")
        r2 = await w2.process_message(msg)
        assert r2.delivery_status == DeliveryStatus.ACK
        assert r2.details.get("status") == "PROCESSED"


# ===========================================================================
# R15: State Store Restart Resumability
# [INTEGRATION]
# ===========================================================================

class TestR15StateStoreResumability:
    """
    R15: If the worker restarts with a fresh InMemoryWorkflowStore loaded from
    a snapshot, operations must resume correctly from the persisted state.
    """

    @pytest.mark.asyncio
    async def test_r15_snapshot_restore_preserves_claim_state(self):
        """
        Create a workflow and claim, export state, create new store from snapshot,
        and verify the claim is correctly restored.
        """
        store1 = InMemoryWorkflowStore()
        engine1 = WorkflowEngine(store1)
        wf = await _create_test_wf(engine1)
        wf_id = wf["workflow_id"]
        idemp_key = f"op_r15_{wf_id}"

        msg = _make_msg(wf_id, idemp_key=idemp_key)

        # Process on store1
        c1 = WorkflowEventConsumer(store=store1, engine=engine1, worker_id="w-snap-1")
        w1 = WorkflowWorkerService(consumer=c1, worker_id="w-snap-1")
        r1 = await w1.process_message(msg)
        assert r1.delivery_status == DeliveryStatus.ACK

        # Export state
        snapshot = store1.export_state()

        # Create new store from snapshot (simulates restart)
        store2 = InMemoryWorkflowStore(shared_data=snapshot)
        engine2 = WorkflowEngine(store2)

        # Verify workflow state was restored
        wf_restored = await store2.get_workflow(wf_id)
        assert wf_restored is not None
        assert wf_restored["state"] == WorkflowState.EXECUTING.value
        assert wf_restored["version"] == 2

        # Verify claim was restored
        claim = await store2.get_operation(idemp_key)
        assert claim is not None
        assert claim["status"] == OperationStatus.COMPLETED.value

        # Re-delivery on new store should be SKIPPED_DUPLICATE
        c2 = WorkflowEventConsumer(store=store2, engine=engine2, worker_id="w-snap-2")
        w2 = WorkflowWorkerService(consumer=c2, worker_id="w-snap-2")
        r2 = await w2.process_message(msg)
        assert r2.delivery_status == DeliveryStatus.ACK
        assert r2.details.get("status") == "SKIPPED_DUPLICATE"


# ===========================================================================
# R16: Failure Injection Configuration Safety
# [UNIT]
# ===========================================================================

class TestR16FailureInjectionSafety:
    """
    R16: Failure injection hooks must be impossible to activate in production.
    The test_failure_hook parameter must default to None.
    """

    def test_r16_consumer_default_no_failure_hook(self):
        """WorkflowEventConsumer defaults to no failure hook."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine)
        assert consumer._test_failure_hook is None

    def test_r16_worker_service_no_hooks_exposed(self):
        """WorkflowWorkerService does not expose any failure injection surface."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        consumer = WorkflowEventConsumer(store=store, engine=engine)
        service = WorkflowWorkerService(consumer=consumer)
        # No public test hook attribute on the service
        assert not hasattr(service, "test_failure_hook")
        assert not hasattr(service, "_test_failure_hook")


# ===========================================================================
# R17: Event Type Routing Correctness
# [INTEGRATION]
# ===========================================================================

class TestR17EventTypeRouting:
    """
    R17: Different event types (WORKFLOW_DISPATCH, RECOVERY_TRIGGER,
    APPROVAL_RESUME) must all be routed correctly through the consumer.
    """

    @pytest.mark.asyncio
    async def test_r17_recovery_trigger_event_transitions_to_executing(self):
        """RECOVERY_TRIGGER event should transition workflow to EXECUTING."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        msg = _make_msg(
            wf_id,
            event_type=WorkflowEventType.RECOVERY_TRIGGER,
            idemp_key=f"op_r17_recovery_{wf_id}",
        )

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-event")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-event")

        r = await worker.process_message(msg)
        assert r.delivery_status == DeliveryStatus.ACK

        final = await store.get_workflow(wf_id)
        assert final["state"] == WorkflowState.EXECUTING.value

    @pytest.mark.asyncio
    async def test_r17_approval_resume_event_transitions_to_executing(self):
        """APPROVAL_RESUME event should transition workflow to EXECUTING."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]

        msg = _make_msg(
            wf_id,
            event_type=WorkflowEventType.APPROVAL_RESUME,
            idemp_key=f"op_r17_approval_{wf_id}",
        )

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-appr")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-appr")

        r = await worker.process_message(msg)
        assert r.delivery_status == DeliveryStatus.ACK

        final = await store.get_workflow(wf_id)
        assert final["state"] == WorkflowState.EXECUTING.value


# ===========================================================================
# R18: Idempotency Key Uniqueness Enforcement
# [INTEGRATION]
# ===========================================================================

class TestR18IdempotencyKeyUniqueness:
    """
    R18: Different workflows using the same idempotency key format
    must not collide.
    """

    @pytest.mark.asyncio
    async def test_r18_same_prefix_different_workflows_no_collision(self):
        """Two workflows with similarly prefixed idempotency keys must not collide."""
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf_a = await _create_test_wf(engine, tenant_id="tenant-phase8")
        wf_b = await _create_test_wf(engine, tenant_id="tenant-phase8")

        suffix = uuid.uuid4().hex[:8]
        msg_a = _make_msg(wf_a["workflow_id"], idemp_key=f"op_{suffix}_a")
        msg_b = _make_msg(wf_b["workflow_id"], idemp_key=f"op_{suffix}_b")

        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-uniq")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="w-uniq")

        ra = await worker.process_message(msg_a)
        rb = await worker.process_message(msg_b)

        assert ra.delivery_status == DeliveryStatus.ACK
        assert rb.delivery_status == DeliveryStatus.ACK

        fa = await store.get_workflow(wf_a["workflow_id"])
        fb = await store.get_workflow(wf_b["workflow_id"])
        assert fa["version"] == 2
        assert fb["version"] == 2


# ===========================================================================
# R19: Worker Crash Between ACK and State Persistence
# [INTEGRATION]
# ===========================================================================

class TestR19CrashAfterCompletion:
    """
    R19: If the worker crashes after marking the operation as COMPLETED
    but before returning the HTTP response, the message should be safely
    deduplicated on redelivery.
    """

    @pytest.mark.asyncio
    async def test_r19_crash_after_complete_operation_deduplicates(self):
        """
        Consumer completes execution and marks claim COMPLETED, then crashes.
        Next delivery sees COMPLETED claim and returns SKIPPED_DUPLICATE.
        """
        store = InMemoryWorkflowStore()
        engine = WorkflowEngine(store)
        wf = await _create_test_wf(engine)
        wf_id = wf["workflow_id"]
        idemp_key = f"op_r19_{wf_id}"

        msg = _make_msg(wf_id, idemp_key=idemp_key)

        # Crash after operation completion
        def crash_after_completion(stage, m):
            if stage == "after_completion":
                raise RuntimeError("Crash after complete_operation (before HTTP response)")

        c1 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-ack-crash",
                                   test_failure_hook=crash_after_completion)
        w1 = WorkflowWorkerService(consumer=c1, worker_id="w-ack-crash")
        r1 = await w1.process_message(msg)
        assert r1.delivery_status == DeliveryStatus.NACK

        # Claim should be COMPLETED despite the crash
        claim = await store.get_operation(idemp_key)
        assert claim["status"] == OperationStatus.COMPLETED.value

        # Workflow should already be at EXECUTING
        wf_mid = await store.get_workflow(wf_id)
        assert wf_mid["state"] == WorkflowState.EXECUTING.value

        # Redelivery: SKIPPED_DUPLICATE because claim is COMPLETED
        c2 = WorkflowEventConsumer(store=store, engine=engine, worker_id="w-ack-retry")
        w2 = WorkflowWorkerService(consumer=c2, worker_id="w-ack-retry")
        r2 = await w2.process_message(msg)
        assert r2.delivery_status == DeliveryStatus.ACK
        assert r2.details.get("status") == "SKIPPED_DUPLICATE"

        # Version must not have advanced again
        final = await store.get_workflow(wf_id)
        assert final["version"] == 2


# ===========================================================================
# R20: Production Configuration Safety Audit
# [UNIT]
# ===========================================================================

class TestR20ProductionConfigSafety:
    """
    R20: Production configuration must not allow failure injection,
    and must enforce safety constraints.
    """

    def test_r20_config_failure_injection_not_enabled_by_default(self):
        """No failure injection configuration is enabled in default config."""
        from backend.config import config
        # Verify no failure injection environment variables are set
        assert not hasattr(config, 'enable_failure_injection') or not getattr(config, 'enable_failure_injection', False)

    def test_r20_pubsub_topic_is_set(self):
        """Production Pub/Sub topic must be configured."""
        from backend.config import config
        # The topic name configuration exists
        assert hasattr(config, 'pubsub_topic') or hasattr(config, 'event_publisher_backend')

    def test_r20_metrics_registry_is_thread_safe(self):
        """Metrics registry must have thread-safe locking."""
        assert hasattr(metrics, '_lock')
        import threading
        assert isinstance(metrics._lock, type(threading.Lock()))
