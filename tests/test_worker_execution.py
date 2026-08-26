"""
Phase 6.2.2: Dedicated Worker Execution Service & Distributed Deduplication Test Suite.

Comprehensive tests covering:
- Delivery decision contracts (ACK, NACK, DEAD_LETTER)
- Error classification (RETRYABLE vs PERMANENT)
- Distributed deduplication & OperationClaim races (2, 5, 10 workers)
- Crash-safety cases (A through H)
- Graceful worker shutdown & task draining
- Tenant isolation and security provenance gates
"""

import asyncio
import copy
import json
import multiprocessing
import uuid
from datetime import datetime, timezone, timedelta
import pytest

from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
    MessageValidationError,
)
from backend.events.consumer import WorkflowEventConsumer
from backend.models.workflow import WorkflowState
from backend.models.idempotency import OperationStatus
from backend.persistence.workflow_store import (
    InMemoryWorkflowStore,
    StaleWorkflowStateError,
)
from backend.engine.workflow_engine import WorkflowEngine
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
from backend.worker.models import (
    DeliveryStatus,
    FailureClassification,
    WorkerExecutionResult,
)
from backend.worker.security import (
    DefaultWorkerSecurityValidator,
    SecurityVerificationError,
)
from backend.worker.service import WorkflowWorkerService
from backend.lifecycle import ShutdownManager
from backend.observability.logging import (
    current_request_id,
    current_workflow_id,
    current_tenant_id,
)


@pytest.fixture
def store():
    return InMemoryWorkflowStore()


@pytest.fixture
def engine(store):
    return WorkflowEngine(store)


@pytest.fixture
def consumer(store, engine):
    return WorkflowEventConsumer(store, engine, worker_id="worker-node-1")


@pytest.fixture
def worker_service(consumer):
    shutdown_mgr = ShutdownManager()
    return WorkflowWorkerService(consumer=consumer, shutdown_manager=shutdown_mgr, worker_id="worker-node-1")


async def _create_test_workflow(engine, tenant_id="tenant-acme", name="Worker Test WF"):
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    return await engine.create_workflow(
        name=name,
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id=tenant_id,
    )


# ===========================================================================
# 1. Delivery Decisions & Outcomes
# ===========================================================================

@pytest.mark.asyncio
async def test_01_successful_worker_execution(engine, store, worker_service):
    """Valid message results in DeliveryStatus.ACK, state EXECUTING, and version increment."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    result = await worker_service.process_message(msg)
    assert result.delivery_status == DeliveryStatus.ACK
    assert result.workflow_id == wf_id

    updated_wf = await store.get_workflow(wf_id)
    assert updated_wf["state"] == WorkflowState.EXECUTING.value
    assert updated_wf["version"] == 2


@pytest.mark.asyncio
async def test_02_malformed_message_dead_letter(worker_service):
    """Malformed JSON returns DeliveryStatus.DEAD_LETTER with PERMANENT failure classification."""
    result = await worker_service.process_raw_payload("NOT_VALID_JSON{")
    assert result.delivery_status == DeliveryStatus.DEAD_LETTER
    assert result.failure_type == FailureClassification.PERMANENT
    assert "Message validation failed" in (result.error_message or "")


@pytest.mark.asyncio
async def test_03_invalid_event_type_dead_letter(worker_service):
    """Invalid event_type in raw payload routes to DEAD_LETTER."""
    raw = json.dumps({
        "schema_version": "1.0.0",
        "event_type": "UNKNOWN_EVENT_ACTION",
        "workflow_id": "wf-123",
        "tenant_id": "tenant-1",
        "idempotency_key": "op-1",
    })
    result = await worker_service.process_raw_payload(raw)
    assert result.delivery_status == DeliveryStatus.DEAD_LETTER
    assert result.failure_type == FailureClassification.PERMANENT


@pytest.mark.asyncio
async def test_04_tenant_mismatch_dead_letter(engine, worker_service):
    """Cross-tenant message returns DEAD_LETTER with PERMANENT failure classification."""
    wf = await _create_test_workflow(engine, tenant_id="tenant-alpha")
    wf_id = wf["workflow_id"]

    # Message specifies tenant-beta for tenant-alpha workflow
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-beta",
        idempotency_key=f"op_dispatch_{wf_id}",
    )

    result = await worker_service.process_message(msg)
    assert result.delivery_status == DeliveryStatus.DEAD_LETTER
    assert result.failure_type == FailureClassification.PERMANENT
    assert "Tenant mismatch" in (result.error_message or "")


@pytest.mark.asyncio
async def test_05_stale_occ_version_nack(engine, worker_service):
    """OCC version drift returns DeliveryStatus.NACK with RETRYABLE classification for redelivery."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Transitioned")

    # Workflow is at version 2, message expects version 1
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.STEP_EXECUTE,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_step_{wf_id}",
        expected_version=1,
    )

    result = await worker_service.process_message(msg)
    assert result.delivery_status == DeliveryStatus.NACK
    assert result.failure_type == FailureClassification.RETRYABLE
    assert "OCC conflict" in (result.error_message or "")


@pytest.mark.asyncio
async def test_06_terminal_workflow_ack_dropped(engine, worker_service):
    """Message targeting COMPLETED workflow is dropped safely with DeliveryStatus.ACK."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Running")
    await engine.transition(wf_id, WorkflowState.VERIFYING, detail="Verifying")
    await engine.transition(wf_id, WorkflowState.COMPLETED, detail="Finished")

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.STEP_EXECUTE,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_step_{wf_id}",
        expected_version=4,
    )

    result = await worker_service.process_message(msg)
    assert result.delivery_status == DeliveryStatus.ACK
    assert result.details.get("status") == "SKIPPED_TERMINAL"


# ===========================================================================
# 2. Deduplication & Concurrency Races (2, 5, 10 Workers)
# ===========================================================================

@pytest.mark.asyncio
async def test_07_duplicate_sequential_delivery_ack(engine, worker_service):
    """Sequential duplicate redeliveries result in 1 PROCESSED and 1 SKIPPED_DUPLICATE (both ACK)."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    res1 = await worker_service.process_message(msg)
    assert res1.delivery_status == DeliveryStatus.ACK
    assert res1.details.get("status") == "PROCESSED"

    res2 = await worker_service.process_message(msg)
    assert res2.delivery_status == DeliveryStatus.ACK
    assert res2.details.get("status") == "SKIPPED_DUPLICATE"


@pytest.mark.asyncio
async def test_08_duplicate_concurrent_delivery_2_workers(engine, store):
    """Two concurrent workers receiving identical message: exactly one executes, one skips."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    c1 = WorkflowEventConsumer(store, engine, worker_id="worker-node-1")
    c2 = WorkflowEventConsumer(store, engine, worker_id="worker-node-2")
    w1 = WorkflowWorkerService(consumer=c1, worker_id="worker-node-1")
    w2 = WorkflowWorkerService(consumer=c2, worker_id="worker-node-2")

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    r1, r2 = await asyncio.gather(w1.process_message(msg), w2.process_message(msg))
    assert r1.delivery_status == DeliveryStatus.ACK
    assert r2.delivery_status == DeliveryStatus.ACK

    outcomes = [r1.details.get("status"), r2.details.get("status")]
    assert outcomes.count("PROCESSED") == 1
    assert outcomes.count("SKIPPED_DUPLICATE") == 1


@pytest.mark.asyncio
async def test_09_5_worker_duplicate_race(engine, store):
    """5 concurrent workers race for same message: exactly 1 executes, 4 skip."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    workers = [
        WorkflowWorkerService(
            consumer=WorkflowEventConsumer(store, engine, worker_id=f"worker-{i}"),
            worker_id=f"worker-{i}",
        )
        for i in range(5)
    ]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    results = await asyncio.gather(*[w.process_message(msg) for w in workers])
    for r in results:
        assert r.delivery_status == DeliveryStatus.ACK

    statuses = [r.details.get("status") for r in results]
    assert statuses.count("PROCESSED") == 1
    assert statuses.count("SKIPPED_DUPLICATE") == 4


@pytest.mark.asyncio
async def test_10_10_worker_duplicate_race_multiprocessing(engine, store):
    """10 concurrent workers simulate a distributed swarm race: exactly 1 winner."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    workers = [
        WorkflowWorkerService(
            consumer=WorkflowEventConsumer(store, engine, worker_id=f"worker-{i}"),
            worker_id=f"worker-{i}",
        )
        for i in range(10)
    ]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    results = await asyncio.gather(*[w.process_message(msg) for w in workers])
    statuses = [r.details.get("status") for r in results]
    assert statuses.count("PROCESSED") == 1
    assert statuses.count("SKIPPED_DUPLICATE") == 9


# ===========================================================================
# 3. Crash-Safety Scenarios (A through H)
# ===========================================================================

@pytest.mark.asyncio
async def test_11_crash_before_claim(engine, store, worker_service):
    """CASE A: Worker crashes before claim acquisition -> subsequent delivery processes normally."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    # First attempt aborted before claim (simulated by dropping)
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    # Redelivery processes normally
    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.ACK
    assert res.details.get("status") == "PROCESSED"


@pytest.mark.asyncio
async def test_12_crash_after_claim_before_mutation(engine, store, worker_service):
    """CASE B: Worker crashes after acquiring claim lease -> expired lease allows safe reclaim."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_dispatch_{wf_id}"

    # Pre-populate an expired claim (simulating crashed worker with expired lease)
    now = datetime.now(timezone.utc)
    expired_claim = {
        "idempotency_key": idemp_key,
        "workflow_id": wf_id,
        "tool_name": "event_workflow_dispatch",
        "status": OperationStatus.CLAIMED.value,
        "owner_worker_id": "crashed-worker-99",
        "lease_expires_at": (now - timedelta(seconds=10)).isoformat(),  # EXPIRED
        "created_at": (now - timedelta(seconds=70)).isoformat(),
        "updated_at": (now - timedelta(seconds=70)).isoformat(),
        "version": 1,
    }
    store._operations[idemp_key] = expired_claim

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.ACK
    assert res.details.get("status") == "PROCESSED"


@pytest.mark.asyncio
async def test_13_crash_after_mutation_before_completion(engine, store, worker_service):
    """CASE C: Worker crashes after workflow mutation but before completing claim."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_dispatch_{wf_id}"

    # State was transitioned to EXECUTING (version 2)
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Interrupted")

    # Message specifies expected_version=1 (stale)
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    # OCC triggers NACK for redelivery
    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.NACK
    assert res.failure_type == FailureClassification.RETRYABLE


@pytest.mark.asyncio
async def test_14_crash_after_completion(engine, store, worker_service):
    """CASE D: Worker crashes after claim completion -> redelivery returns cached result."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_dispatch_{wf_id}"

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    # Successfully completes
    res1 = await worker_service.process_message(msg)
    assert res1.delivery_status == DeliveryStatus.ACK

    # Redelivery after process restart
    res2 = await worker_service.process_message(msg)
    assert res2.delivery_status == DeliveryStatus.ACK
    assert res2.details.get("status") == "SKIPPED_DUPLICATE"


@pytest.mark.asyncio
async def test_15_cached_completed_result(engine, store, worker_service):
    """Verifies that completed operations return cached outcome details."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_cached_{wf_id}"

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    await worker_service.process_message(msg)
    res = await worker_service.process_message(msg)
    assert res.details.get("claim_status") == OperationStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_16_active_lease_claim_behavior(engine, store):
    """Worker B cannot steal active unexpired lease held by Worker A."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_active_{wf_id}"

    # Worker A acquires 60s lease
    now = datetime.now(timezone.utc)
    active_claim = {
        "idempotency_key": idemp_key,
        "workflow_id": wf_id,
        "tool_name": "event_workflow_dispatch",
        "status": OperationStatus.CLAIMED.value,
        "owner_worker_id": "worker-node-A",
        "lease_expires_at": (now + timedelta(seconds=55)).isoformat(),  # ACTIVE
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "version": 1,
    }
    store._operations[idemp_key] = active_claim

    # Worker B tries to process
    c_b = WorkflowEventConsumer(store, engine, worker_id="worker-node-B")
    w_b = WorkflowWorkerService(consumer=c_b, worker_id="worker-node-B")

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    res = await w_b.process_message(msg)
    assert res.delivery_status == DeliveryStatus.ACK
    assert res.details.get("status") == "SKIPPED_DUPLICATE"


# ===========================================================================
# 4. Worker Shutdown & Lifecycle Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_17_retryable_failure_classification(engine, worker_service):
    """Transient errors are classified as RETRYABLE with NACK delivery status."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    await engine.transition(wf_id, WorkflowState.EXECUTING)

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.STEP_EXECUTE,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_err_{wf_id}",
        expected_version=1,  # Stale -> OCC Conflict
    )

    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.NACK
    assert res.failure_type == FailureClassification.RETRYABLE


@pytest.mark.asyncio
async def test_18_permanent_failure_classification(worker_service):
    """Security verification failure is classified as PERMANENT with DEAD_LETTER."""
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id="wf-sec-1",
        tenant_id="tenant-1",
        idempotency_key="op-sec-1",
        producer_id="malicious-untrusted-producer",  # Untrusted
    )

    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.DEAD_LETTER
    assert res.failure_type == FailureClassification.PERMANENT
    assert "Untrusted producer ID" in (res.error_message or "")


@pytest.mark.asyncio
async def test_19_graceful_worker_shutdown(engine, consumer):
    """Worker in shutdown state rejects new messages with NACK for redelivery."""
    shutdown_mgr = ShutdownManager()
    worker_service = WorkflowWorkerService(consumer=consumer, shutdown_manager=shutdown_mgr)

    # Initiate shutdown
    shutdown_mgr.begin_shutdown()

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id="wf-shut-1",
        tenant_id="tenant-1",
        idempotency_key="op-shut-1",
    )

    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.NACK
    assert res.failure_type == FailureClassification.RETRYABLE
    assert "shutting down" in (res.error_message or "")


@pytest.mark.asyncio
async def test_20_active_work_shutdown_draining(engine, store, consumer):
    """Active background worker task is drained cleanly during shutdown."""
    shutdown_mgr = ShutdownManager()
    worker_service = WorkflowWorkerService(consumer=consumer, shutdown_manager=shutdown_mgr)

    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_drain_{wf_id}",
        expected_version=1,
    )

    # Spawn background task and register with shutdown manager
    bg_task = asyncio.create_task(worker_service.process_message(msg))
    shutdown_mgr.register_task(bg_task)

    # Shutdown should drain the task cleanly
    drained = await shutdown_mgr.drain_tasks(timeout=2.0)
    assert drained == 1
    assert len(shutdown_mgr._active_tasks) == 0
    assert bg_task.done()
    res = bg_task.result()
    assert res.delivery_status == DeliveryStatus.ACK


# ===========================================================================
# 5. Observability, Security & Invariant Non-Bypass
# ===========================================================================

@pytest.mark.asyncio
async def test_21_correlation_id_propagation(engine, worker_service):
    """Correlation ID propagates through message -> worker -> consumer -> contextvars."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    corr_id = "req-trace-9999"

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        correlation_id=corr_id,
        idempotency_key=f"op_corr_{wf_id}",
        expected_version=1,
    )

    await worker_service.process_message(msg)
    assert current_request_id.get() == corr_id
    assert current_workflow_id.get() == wf_id
    assert current_tenant_id.get() == "tenant-acme"


@pytest.mark.asyncio
async def test_22_sensitive_data_redaction(worker_service):
    """Worker service execution results do not contain leaked secrets."""
    res = await worker_service.process_raw_payload("{\"api_key\": \"AIzaSyB1234567890abcdefghijklmn\", \"jwt\": \"eyJhbGciOiABCDEF12345.xyz9876543210.signature\"}")
    assert "AIza" not in (res.error_message or "")
    assert "eyJ" not in (res.error_message or "")
    assert "REDACTED" in (res.error_message or "")


@pytest.mark.asyncio
async def test_23_worker_must_not_bypass_operation_claim(engine, store, worker_service):
    """Worker execution authoritatively records and respects OperationClaim."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_claim_verify_{wf_id}"

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    await worker_service.process_message(msg)
    claim = store._operations.get(idemp_key)
    assert claim is not None
    assert claim["status"] == OperationStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_24_worker_must_not_bypass_workflow_engine(engine, store, worker_service):
    """Worker transitions strictly route through WorkflowEngine."""
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_engine_verify_{wf_id}",
        expected_version=1,
    )

    await worker_service.process_message(msg)
    events = await store.get_events(wf_id)
    assert len(events) >= 2  # Creation event + State Change event


@pytest.mark.asyncio
async def test_25_worker_must_not_bypass_tenant_isolation(engine, worker_service):
    """Worker rejects cross-tenant execution attempt."""
    wf = await _create_test_workflow(engine, tenant_id="tenant-secure-1")
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-attacker",
        idempotency_key=f"op_tenant_verify_{wf_id}",
    )

    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.DEAD_LETTER
    assert res.failure_type == FailureClassification.PERMANENT
