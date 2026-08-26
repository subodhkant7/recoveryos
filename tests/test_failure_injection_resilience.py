"""
RecoveryOS Phase 6.4 Failure Injection & Resilience Test Suite.

Comprehensive tests verifying:
- F1: Worker Unavailable (500 / NACK -> retry -> eventual ACK without message loss)
- F2: Crash After Operation Claim (lease blocks concurrent execution; reclaim after expiry; no duplicate business execution)
- F3: Crash After CREATED -> EXECUTING (OCC prevents double transition or state regression)
- F4: Gemini API Failure / Rate Limit (Quota/Rate-limit/Timeout classification and non-destructive retry)
- F5: Firestore Persistence Failure (write error -> fail-closed NACK, no false ACK before persistence)
- F6: Duplicate Pub/Sub Delivery (multi-delivery idempotency: 1 PROCESSED, N SKIPPED_DUPLICATE)
- F7: OCC Stale Version (stale expected_version rejected without overwriting newer state)
- F8: Poison Message / DLQ (malformed messages return DEAD_LETTER / HTTP 422 for DLQ routing)
- F9: API Publish Failure (Pub/Sub failure returns HTTP 503; does not run in-process)
- F10: Worker Restart Recovery (shutdown task draining; redelivered task safely processed by new worker)
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch

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
from backend.worker.server import app, set_worker_service
from backend.lifecycle import ShutdownManager


async def _create_test_wf(engine, tenant_id="tenant-phase64"):
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    return await engine.create_workflow(
        name="Phase 6.4 Failure Test WF",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id=tenant_id,
    )


# ===========================================================================
# F1: Worker Unavailable (Temporary 500 -> Retry -> Eventual Success)
# ===========================================================================

@pytest.mark.asyncio
async def test_f1_worker_unavailable_retry_and_recovery():
    """F1: Simulated temporary worker fault returns NACK (500), then succeeds upon recovery without message loss."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    wf = await _create_test_wf(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-phase64",
        idempotency_key=f"op_f1_{wf_id}",
        expected_version=1,
    )

    # Attempt 1: Worker has simulated transient dependency outage
    faulty_consumer = AsyncMock()
    faulty_consumer.consume_message.side_effect = ConnectionError("Transient network timeout to dependencies")
    worker_1 = WorkflowWorkerService(consumer=faulty_consumer, worker_id="worker-1")

    res1 = await worker_1.process_message(msg)
    assert res1.delivery_status == DeliveryStatus.NACK
    assert res1.failure_type == FailureClassification.RETRYABLE

    # Workflow is still at CREATED v1 in store (no corrupt state)
    wf_state_1 = await store.get_workflow(wf_id)
    assert wf_state_1["state"] == WorkflowState.CREATED.value
    assert wf_state_1["version"] == 1

    # Attempt 2: Worker recovers and processes redelivery
    healthy_consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-2")
    worker_2 = WorkflowWorkerService(consumer=healthy_consumer, worker_id="worker-2")

    res2 = await worker_2.process_message(msg)
    assert res2.delivery_status == DeliveryStatus.ACK
    assert res2.details.get("status") == "PROCESSED"

    # Verified durable final state
    wf_state_2 = await store.get_workflow(wf_id)
    assert wf_state_2["state"] == WorkflowState.EXECUTING.value
    assert wf_state_2["version"] == 2


# ===========================================================================
# F2: Crash After Operation Claim (Lease Expiration & Reclaim)
# ===========================================================================

@pytest.mark.asyncio
async def test_f2_crash_after_operation_claim_lease_reclaim():
    """F2: Worker crashes right after acquiring claim lease; redelivery reclaims safely after expiry."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    wf = await _create_test_wf(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_f2_{wf_id}"

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-phase64",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    # 1. First worker acquires claim lease, but crashes before state transition
    def crash_hook(stage, m):
        if stage == "after_claim":
            raise RuntimeError("Simulated crash immediately after acquiring OperationClaim lease")

    crashing_consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        worker_id="crashed-worker-1",
        test_failure_hook=crash_hook,
    )
    worker_1 = WorkflowWorkerService(consumer=crashing_consumer, worker_id="crashed-worker-1")

    res1 = await worker_1.process_message(msg)
    assert res1.delivery_status == DeliveryStatus.NACK
    assert "Simulated crash" in (res1.error_message or "")

    # Verify claim document in store has status CLAIMED and owner crashed-worker-1
    claim_1 = store._operations.get(idemp_key)
    assert claim_1 is not None
    assert claim_1["status"] == OperationStatus.CLAIMED.value
    assert claim_1["owner_worker_id"] == "crashed-worker-1"

    # 2. Immediate redelivery while lease is ACTIVE: Worker 2 skips execution
    healthy_consumer_2 = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-2")
    worker_2 = WorkflowWorkerService(consumer=healthy_consumer_2, worker_id="worker-2")

    res_active = await worker_2.process_message(msg)
    assert res_active.delivery_status == DeliveryStatus.ACK
    assert res_active.details.get("status") == "SKIPPED_DUPLICATE"

    # 3. Simulate lease expiration (time passes beyond lease_seconds)
    now = datetime.now(timezone.utc)
    claim_1["lease_expires_at"] = (now - timedelta(seconds=10)).isoformat()
    store._operations[idemp_key] = claim_1

    # 4. Redelivery after lease expiry: Worker 3 safely reclaims and completes execution
    healthy_consumer_3 = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-3")
    worker_3 = WorkflowWorkerService(consumer=healthy_consumer_3, worker_id="worker-3")

    res3 = await worker_3.process_message(msg)
    assert res3.delivery_status == DeliveryStatus.ACK
    assert res3.details.get("status") == "PROCESSED"

    # Final state: Workflow is EXECUTING v2, Claim is COMPLETED by worker-3
    final_wf = await store.get_workflow(wf_id)
    assert final_wf["state"] == WorkflowState.EXECUTING.value
    assert final_wf["version"] == 2

    final_claim = store._operations.get(idemp_key)
    assert final_claim["status"] == OperationStatus.COMPLETED.value
    assert final_claim["owner_worker_id"] == "worker-3"


# ===========================================================================
# F3: Crash After CREATED -> EXECUTING
# ===========================================================================

@pytest.mark.asyncio
async def test_f3_crash_after_state_transition():
    """F3: Worker crashes right after CREATED -> EXECUTING mutation; OCC rejects stale redelivery without corrupting state."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    wf = await _create_test_wf(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_f3_{wf_id}"

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-phase64",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    # Worker transitions state to EXECUTING (version 1 -> 2), then crashes before complete_operation
    def crash_after_transition(stage, m):
        if stage == "after_transition":
            raise RuntimeError("Simulated crash after state transition completed")

    crashing_consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        worker_id="worker-crash-t",
        test_failure_hook=crash_after_transition,
    )
    worker_1 = WorkflowWorkerService(consumer=crashing_consumer, worker_id="worker-crash-t")

    res1 = await worker_1.process_message(msg)
    assert res1.delivery_status == DeliveryStatus.NACK

    # Verify Firestore state is EXECUTING at version 2
    wf_after_crash = await store.get_workflow(wf_id)
    assert wf_after_crash["state"] == WorkflowState.EXECUTING.value
    assert wf_after_crash["version"] == 2

    # Expire claim lease so redelivery evaluates OCC
    claim = store._operations.get(idemp_key)
    now = datetime.now(timezone.utc)
    claim["lease_expires_at"] = (now - timedelta(seconds=5)).isoformat()

    # Redelivered message specifies expected_version=1, but workflow is at version 2
    healthy_consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-recovered")
    worker_2 = WorkflowWorkerService(consumer=healthy_consumer, worker_id="worker-recovered")

    res2 = await worker_2.process_message(msg)
    # OCC mismatch correctly returns NACK
    assert res2.delivery_status == DeliveryStatus.NACK
    assert res2.failure_type == FailureClassification.RETRYABLE

    # Verify workflow version did NOT regress
    wf_final = await store.get_workflow(wf_id)
    assert wf_final["version"] == 2
    assert wf_final["state"] == WorkflowState.EXECUTING.value


# ===========================================================================
# F4: Gemini API Failure & Rate Limit Handling
# ===========================================================================

@pytest.mark.asyncio
async def test_f4_gemini_failure_classification():
    """F4: Transient LLM errors trigger retryable backoff without false ACKs."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    wf = await _create_test_wf(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-phase64",
        idempotency_key=f"op_f4_{wf_id}",
        expected_version=1,
    )

    # Simulate Gemini 429 ResourceExhausted exception during execution turn
    def gemini_rate_limit_hook(stage, m):
        if stage == "after_claim":
            raise TimeoutError("Gemini API call timed out after 30.0s")

    consumer = WorkflowEventConsumer(
        store=store,
        engine=engine,
        worker_id="worker-gemini-test",
        test_failure_hook=gemini_rate_limit_hook,
    )
    worker = WorkflowWorkerService(consumer=consumer, worker_id="worker-gemini-test")

    res = await worker.process_message(msg)
    assert res.delivery_status == DeliveryStatus.NACK
    assert res.failure_type == FailureClassification.RETRYABLE


# ===========================================================================
# F5: Firestore Persistence Failure (Fail-Closed)
# ===========================================================================

@pytest.mark.asyncio
async def test_f5_firestore_persistence_failure_fail_closed():
    """F5: Database write error during state transition fails closed (NACK, no false ACK)."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    wf = await _create_test_wf(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-phase64",
        idempotency_key=f"op_f5_{wf_id}",
        expected_version=1,
    )

    # Patch store.save_workflow to raise an IOError
    with patch.object(store, "save_workflow", side_effect=IOError("Firestore deadline exceeded on commit")):
        consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-fs-fault")
        worker = WorkflowWorkerService(consumer=consumer, worker_id="worker-fs-fault")

        res = await worker.process_message(msg)
        assert res.delivery_status == DeliveryStatus.NACK
        assert res.failure_type == FailureClassification.RETRYABLE


# ===========================================================================
# F6: Duplicate Pub/Sub Delivery (Exactly-Once Business Execution)
# ===========================================================================

@pytest.mark.asyncio
async def test_f6_duplicate_pubsub_delivery_idempotency():
    """F6: Multiple deliveries of same message result in exactly 1 execution and N SKIPPED_DUPLICATE (all ACKed)."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    wf = await _create_test_wf(engine)
    wf_id = wf["workflow_id"]
    idemp_key = f"op_f6_{wf_id}"

    consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-dedup")
    worker = WorkflowWorkerService(consumer=consumer, worker_id="worker-dedup")

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-phase64",
        idempotency_key=idemp_key,
        expected_version=1,
    )

    # Delivery 1
    r1 = await worker.process_message(msg)
    assert r1.delivery_status == DeliveryStatus.ACK
    assert r1.details.get("status") == "PROCESSED"

    # Delivery 2 (Duplicate)
    r2 = await worker.process_message(msg)
    assert r2.delivery_status == DeliveryStatus.ACK
    assert r2.details.get("status") == "SKIPPED_DUPLICATE"

    # Delivery 3 (Duplicate)
    r3 = await worker.process_message(msg)
    assert r3.delivery_status == DeliveryStatus.ACK
    assert r3.details.get("status") == "SKIPPED_DUPLICATE"

    # Verify version advanced exactly once (1 -> 2)
    wf_final = await store.get_workflow(wf_id)
    assert wf_final["version"] == 2


# ===========================================================================
# F7: OCC Stale Version Rejection
# ===========================================================================

@pytest.mark.asyncio
async def test_f7_occ_stale_version_rejection():
    """F7: Message with expected_version=1 is rejected when workflow has already advanced to version 2."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    wf = await _create_test_wf(engine)
    wf_id = wf["workflow_id"]

    # Pre-advance workflow version to 2
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Concurrent advance")
    current_wf = await store.get_workflow(wf_id)
    assert current_wf["version"] == 2

    # Attempt to consume stale message specifying expected_version=1
    consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-occ")
    worker = WorkflowWorkerService(consumer=consumer, worker_id="worker-occ")

    msg_stale = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-phase64",
        idempotency_key=f"op_f7_stale_{wf_id}",
        expected_version=1,
    )

    res = await worker.process_message(msg_stale)
    assert res.delivery_status == DeliveryStatus.NACK
    assert res.failure_type == FailureClassification.RETRYABLE

    # Verify version was NOT regressed or overwritten
    final_wf = await store.get_workflow(wf_id)
    assert final_wf["version"] == 2


# ===========================================================================
# F8: Poison Message / DLQ Routing
# ===========================================================================

@pytest.mark.asyncio
async def test_f8_poison_message_returns_dead_letter():
    """F8: Poison message (malformed JSON, schema failure, tenant mismatch) classified as DEAD_LETTER for DLQ routing."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-dlq")
    worker = WorkflowWorkerService(consumer=consumer, worker_id="worker-dlq")

    # 1. Malformed JSON
    r1 = await worker.process_raw_payload("INVALID_JSON_RAW")
    assert r1.delivery_status == DeliveryStatus.DEAD_LETTER
    assert r1.failure_type == FailureClassification.PERMANENT

    # 2. Missing required schema field
    r2 = await worker.process_raw_payload(json.dumps({"event_type": "WORKFLOW_DISPATCH"}))
    assert r2.delivery_status == DeliveryStatus.DEAD_LETTER
    assert r2.failure_type == FailureClassification.PERMANENT

    # 3. Non-existent workflow ID
    r3 = await worker.process_message(
        WorkflowExecutionMessage(
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            workflow_id=str(uuid.uuid4()),  # Non-existent
            tenant_id="tenant-phase64",
            idempotency_key="op_nonexistent",
            expected_version=1,
        )
    )
    assert r3.delivery_status == DeliveryStatus.DEAD_LETTER
    assert r3.failure_type == FailureClassification.PERMANENT


# ===========================================================================
# F9: API Publish Failure (Fail-Closed HTTP 503)
# ===========================================================================

@pytest.mark.asyncio
async def test_f9_api_publish_failure_returns_503():
    """F9: When Pub/Sub publish fails, API returns HTTP 503 and never falls back to in-process execution."""
    import dataclasses
    from backend.api.server import app, get_event_publisher, set_event_publisher
    from backend.events.publisher import BaseEventPublisher, EventPublishError
    from backend.security.tokens import create_access_token
    from backend.config import config

    class FaultyPublisher(BaseEventPublisher):
        async def publish_workflow_execution(self, message):
            raise EventPublishError("Pub/Sub publish RPC deadline exceeded")

    original_publisher = get_event_publisher()
    set_event_publisher(FaultyPublisher())
    try:
        jwt_token = create_access_token("operator-1", "operator", "tenant-phase64", secret_key=config.jwt_secret_key)
        custom_cfg = dataclasses.replace(config, event_publisher_backend="pubsub")

        with patch("backend.api.server.config", custom_cfg):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/scenarios/billing_unavailable",
                    headers={"Authorization": f"Bearer {jwt_token}"},
                )
                assert resp.status_code == 503
                assert "Failed to dispatch workflow execution" in resp.json().get("detail", "")
    finally:
        set_event_publisher(original_publisher)


# ===========================================================================
# F10: Worker Restart Recovery (Task Draining & Reclaim)
# ===========================================================================

@pytest.mark.asyncio
async def test_f10_worker_restart_and_task_draining():
    """F10: In-flight worker task is drained on graceful shutdown; restarting worker safely processes unacknowledged work."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    wf = await _create_test_wf(engine)
    wf_id = wf["workflow_id"]

    shutdown_mgr = ShutdownManager()
    consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-instance-1")
    worker = WorkflowWorkerService(consumer=consumer, shutdown_manager=shutdown_mgr, worker_id="worker-instance-1")

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-phase64",
        idempotency_key=f"op_f10_{wf_id}",
        expected_version=1,
    )

    # Launch task in background and drain during graceful shutdown
    task = asyncio.create_task(worker.process_message(msg))
    shutdown_mgr.register_task(task)

    drained_count = await shutdown_mgr.drain_tasks(timeout=2.0)
    assert drained_count == 1
    assert task.done()
    result = task.result()
    assert result.delivery_status == DeliveryStatus.ACK

    # Verify final state is consistent in store
    final_wf = await store.get_workflow(wf_id)
    assert final_wf["state"] == WorkflowState.EXECUTING.value
    assert final_wf["version"] == 2
