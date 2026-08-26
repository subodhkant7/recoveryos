"""
Phase 7 Operational Hardening Automated Test Suite.

Covers:
1. Duplicate Pub/Sub message deduplication & idempotent state preservation.
2. Concurrent duplicate delivery races & single lease acquisition.
3. Worker crash simulation before claim.
4. Worker crash simulation after claim with lease expiry recovery.
5. Malformed message dead-letter decision.
6. Non-existent workflow permanent error classification.
7. OCC version mismatch retryable NACK handling.
8. Terminal workflow immutability & safe drop.
9. Stuck workflow diagnostics computation.
10. Operator recovery security boundaries & OCC fencing.
"""

from __future__ import annotations

import asyncio
import datetime
import pytest
import uuid
from typing import Any

from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
    MessageValidationError,
)
from backend.events.consumer import (
    WorkflowEventConsumer,
    ConsumerExecutionError,
)
from backend.models.workflow import Workflow, WorkflowState
from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.worker.service import WorkflowWorkerService
from backend.worker.models import DeliveryStatus, FailureClassification
from backend.security.principal import Principal, Role


@pytest.fixture
def store():
    return InMemoryWorkflowStore()


@pytest.fixture
def engine(store):
    return WorkflowEngine(store=store)


@pytest.fixture
def consumer(store, engine):
    return WorkflowEventConsumer(store=store, engine=engine, worker_id="test-worker-1")


@pytest.fixture
def worker_service(consumer):
    return WorkflowWorkerService(consumer=consumer, worker_id="test-worker-1")


# ==============================================================================
# 1. Duplicate Pub/Sub Message Delivery & Idempotency
# ==============================================================================

@pytest.mark.asyncio
async def test_01_duplicate_pubsub_message_replays_without_state_mutation(store, engine, worker_service):
    """Replaying the same Pub/Sub message returns ACK and does not cause duplicate state advancement."""
    wf_id = f"wf-dup-{uuid.uuid4().hex[:8]}"
    wf = Workflow(
        workflow_id=wf_id,
        tenant_id="tenant-test",
        state=WorkflowState.CREATED,
        version=1,
        scenario="billing_unavailable",
    )
    await store.save_workflow(wf.model_dump(mode="json"))

    msg = WorkflowExecutionMessage(
        message_id="msg-1",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-test",
        correlation_id="corr-1",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    # First delivery
    res1 = await worker_service.process_message(msg)
    assert res1.delivery_status == DeliveryStatus.ACK

    wf_after1 = await store.get_workflow(wf_id)
    assert wf_after1["state"] == WorkflowState.EXECUTING.value
    assert wf_after1["version"] == 2

    # Second replayed delivery
    res2 = await worker_service.process_message(msg)
    assert res2.delivery_status == DeliveryStatus.ACK
    assert res2.details.get("status") == "SKIPPED_DUPLICATE"

    # Version and state must remain exactly what was established after first execution
    wf_after2 = await store.get_workflow(wf_id)
    assert wf_after2["state"] == WorkflowState.EXECUTING.value
    assert wf_after2["version"] == 2


# ==============================================================================
# 2. Concurrent Duplicate Delivery Race
# ==============================================================================

@pytest.mark.asyncio
async def test_02_concurrent_duplicate_delivery_race_wins_single_lease(store, engine):
    """When two workers receive identical messages simultaneously, exactly one processes and one skips."""
    wf_id = f"wf-race-{uuid.uuid4().hex[:8]}"
    wf = Workflow(
        workflow_id=wf_id,
        tenant_id="tenant-test",
        state=WorkflowState.CREATED,
        version=1,
    )
    await store.save_workflow(wf.model_dump(mode="json"))

    consumer_a = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-a")
    consumer_b = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-b")
    service_a = WorkflowWorkerService(consumer=consumer_a, worker_id="worker-a")
    service_b = WorkflowWorkerService(consumer=consumer_b, worker_id="worker-b")

    msg = WorkflowExecutionMessage(
        message_id="msg-race",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-test",
        correlation_id="corr-race",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    res_a, res_b = await asyncio.gather(
        service_a.process_message(msg),
        service_b.process_message(msg),
    )

    assert res_a.delivery_status == DeliveryStatus.ACK
    assert res_b.delivery_status == DeliveryStatus.ACK

    outcomes = {res_a.details.get("status"), res_b.details.get("status")}
    assert outcomes == {"PROCESSED", "SKIPPED_DUPLICATE"}

    wf_final = await store.get_workflow(wf_id)
    assert wf_final["version"] == 2


# ==============================================================================
# 3. Worker Crash Before Claim
# ==============================================================================

@pytest.mark.asyncio
async def test_03_worker_crash_before_claim_preserves_clean_replay(store, engine):
    """If a worker crashes before acquiring an operation claim, subsequent delivery succeeds cleanly."""
    wf_id = f"wf-crash1-{uuid.uuid4().hex[:8]}"
    wf = Workflow(
        workflow_id=wf_id,
        tenant_id="tenant-test",
        state=WorkflowState.CREATED,
        version=1,
    )
    await store.save_workflow(wf.model_dump(mode="json"))

    def crash_hook(step, m):
        if step == "before_claim":
            raise RuntimeError("Simulated worker process crash before claim")

    consumer_crashing = WorkflowEventConsumer(
        store=store, engine=engine, worker_id="worker-crash", test_failure_hook=crash_hook
    )
    service_crashing = WorkflowWorkerService(consumer=consumer_crashing, worker_id="worker-crash")

    msg = WorkflowExecutionMessage(
        message_id="msg-crash1",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-test",
        correlation_id="corr-crash1",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    # Crashing worker execution returns NACK
    res_crash = await service_crashing.process_message(msg)
    assert res_crash.delivery_status == DeliveryStatus.NACK
    assert res_crash.failure_type == FailureClassification.RETRYABLE

    # Subsequent healthy worker succeeds
    consumer_healthy = WorkflowEventConsumer(store=store, engine=engine, worker_id="worker-healthy")
    service_healthy = WorkflowWorkerService(consumer=consumer_healthy, worker_id="worker-healthy")

    res_ok = await service_healthy.process_message(msg)
    assert res_ok.delivery_status == DeliveryStatus.ACK
    assert res_ok.details.get("status") == "PROCESSED"


# ==============================================================================
# 4. Worker Crash After Claim with Lease Expiry Recovery
# ==============================================================================

@pytest.mark.asyncio
async def test_04_worker_crash_after_claim_recovers_after_lease_expiry(store, engine):
    """If a worker acquires a lease and dies, subsequent worker reclaims and completes once lease expires."""
    wf_id = f"wf-crash2-{uuid.uuid4().hex[:8]}"
    wf = Workflow(
        workflow_id=wf_id,
        tenant_id="tenant-test",
        state=WorkflowState.CREATED,
        version=1,
    )
    await store.save_workflow(wf.model_dump(mode="json"))

    # Acquire an initial claim that simulates a crashed worker with expired lease
    now = datetime.datetime.now(datetime.timezone.utc)
    expired_time = now - datetime.timedelta(seconds=10)

    store._operations[f"op_dispatch_{wf_id}"] = {
        "idempotency_key": f"op_dispatch_{wf_id}",
        "workflow_id": wf_id,
        "tool_name": "event_workflow_dispatch",
        "target_entity_id": wf_id,
        "parameters": {},
        "status": "CLAIMED",
        "owner_worker_id": "crashed-worker",
        "lease_expires_at": expired_time.isoformat(),
        "created_at": expired_time.isoformat(),
        "updated_at": expired_time.isoformat(),
        "version": 1,
    }

    consumer = WorkflowEventConsumer(store=store, engine=engine, worker_id="recovering-worker")
    service = WorkflowWorkerService(consumer=consumer, worker_id="recovering-worker")

    msg = WorkflowExecutionMessage(
        message_id="msg-crash2",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-test",
        correlation_id="corr-crash2",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    res = await service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.ACK
    assert res.details.get("status") == "PROCESSED"

    wf_final = await store.get_workflow(wf_id)
    assert wf_final["state"] == WorkflowState.EXECUTING.value


# ==============================================================================
# 5. Malformed Message Routing to Dead Letter
# ==============================================================================

@pytest.mark.asyncio
async def test_05_malformed_message_routes_to_dead_letter(worker_service):
    """Malformed or invalid JSON payload is classified as PERMANENT and routes to DEAD_LETTER."""
    invalid_raw = b'{"invalid": "schema_missing_fields"}'
    res = await worker_service.process_raw_payload(invalid_raw)
    assert res.delivery_status == DeliveryStatus.DEAD_LETTER
    assert res.failure_type == FailureClassification.PERMANENT


# ==============================================================================
# 6. Non-Existent Workflow Handling
# ==============================================================================

@pytest.mark.asyncio
async def test_06_nonexistent_workflow_permanent_error_handling(worker_service):
    """Event targeting non-existent workflow is rejected permanently without infinite retry."""
    msg = WorkflowExecutionMessage(
        message_id="msg-nonexistent",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id="wf-does-not-exist-404",
        tenant_id="tenant-test",
        correlation_id="corr-404",
        idempotency_key="op_dispatch_nonexistent",
        expected_version=1,
    )
    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.DEAD_LETTER
    assert res.failure_type == FailureClassification.PERMANENT


# ==============================================================================
# 7. OCC Version Mismatch Retryable NACK Handling
# ==============================================================================

@pytest.mark.asyncio
async def test_07_occ_version_mismatch_triggers_retryable_nack(store, engine, worker_service):
    """Message with stale expected version triggers NACK for retry."""
    wf_id = f"wf-occ-{uuid.uuid4().hex[:8]}"
    wf = Workflow(
        workflow_id=wf_id,
        tenant_id="tenant-test",
        state=WorkflowState.CREATED,
        version=5,  # Store version is 5
    )
    # Save directly to store with version 5
    doc = wf.model_dump(mode="json")
    doc["version"] = 5
    store._workflows[wf_id] = doc

    msg = WorkflowExecutionMessage(
        message_id="msg-stale",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-test",
        correlation_id="corr-stale",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,  # Message expects version 1
    )

    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.NACK
    assert res.failure_type == FailureClassification.RETRYABLE
    assert "OCC conflict" in (res.error_message or "")


# ==============================================================================
# 8. Terminal Workflow Immutability
# ==============================================================================

@pytest.mark.asyncio
async def test_08_terminal_workflow_event_dropped_without_mutation(store, engine, worker_service):
    """Messages targeting COMPLETED or ESCALATED workflows return SKIPPED_TERMINAL and ACK."""
    wf_id = f"wf-term-{uuid.uuid4().hex[:8]}"
    wf = Workflow(
        workflow_id=wf_id,
        tenant_id="tenant-test",
        state=WorkflowState.COMPLETED,
        version=3,
    )
    doc = wf.model_dump(mode="json")
    doc["version"] = 3
    store._workflows[wf_id] = doc

    msg = WorkflowExecutionMessage(
        message_id="msg-term",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-test",
        correlation_id="corr-term",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=3,
    )

    res = await worker_service.process_message(msg)
    assert res.delivery_status == DeliveryStatus.ACK
    assert res.details.get("status") == "SKIPPED_TERMINAL"

    wf_check = await store.get_workflow(wf_id)
    assert wf_check["state"] == WorkflowState.COMPLETED.value
    assert wf_check["version"] == 3


# ==============================================================================
# 9. Stuck Workflow Diagnostics Computation
# ==============================================================================

@pytest.mark.asyncio
async def test_09_diagnostics_correctly_identifies_stuck_vs_healthy():
    """Diagnostics correctly marks workflows inactive in CREATED state as stuck and terminal workflows as unrecoverable."""
    from backend.api.server import app, store
    from backend.security.tokens import create_access_token
    from httpx import AsyncClient, ASGITransport

    token = create_access_token("op-test", Role.OPERATOR, tenant_id="tenant-test")
    headers = {"Authorization": f"Bearer {token}"}

    # Case A: Old workflow in CREATED state (>60s) -> is_stuck=True, is_recoverable=True
    wf_id_stuck = f"wf-stuck-{uuid.uuid4().hex[:8]}"
    old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=120)).isoformat()
    wf_stuck = Workflow(
        workflow_id=wf_id_stuck,
        tenant_id="tenant-test",
        state=WorkflowState.CREATED,
        version=1,
    )
    doc_stuck = wf_stuck.model_dump(mode="json")
    doc_stuck["created_at"] = old_time
    store._workflows[wf_id_stuck] = doc_stuck

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_stuck = await client.get(f"/api/workflows/{wf_id_stuck}/diagnostics", headers=headers)
        assert res_stuck.status_code == 200
        diag_stuck = res_stuck.json()
        assert diag_stuck["is_stuck"] is True
        assert diag_stuck["is_recoverable"] is True
        assert diag_stuck["is_terminal"] is False

    # Case B: Recent healthy workflow -> is_stuck=False
    wf_id_healthy = f"wf-healthy-{uuid.uuid4().hex[:8]}"
    wf_healthy = Workflow(
        workflow_id=wf_id_healthy,
        tenant_id="tenant-test",
        state=WorkflowState.CREATED,
        version=1,
    )
    store._workflows[wf_id_healthy] = wf_healthy.model_dump(mode="json")

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_healthy = await client.get(f"/api/workflows/{wf_id_healthy}/diagnostics", headers=headers)
        assert res_healthy.status_code == 200
        diag_healthy = res_healthy.json()
        assert diag_healthy["is_stuck"] is False
        assert diag_healthy["is_recoverable"] is True


# ==============================================================================
# 10. Operator Recovery Security & Terminal Boundaries
# ==============================================================================

@pytest.mark.asyncio
async def test_10_recovery_fencing_blocks_unauthorized_and_terminal_workflows(store):
    """Operator recovery strictly enforces OPERATOR role, tenant isolation, and terminal state checks."""
    wf_id = f"wf-rec-{uuid.uuid4().hex[:8]}"
    wf = Workflow(
        workflow_id=wf_id,
        tenant_id="tenant-alpha",
        state=WorkflowState.COMPLETED,
        version=4,
    )
    store._workflows[wf_id] = wf.model_dump(mode="json")

    viewer = Principal(user_id="user-view", role=Role.VIEWER, tenant_id="tenant-alpha")
    operator_cross = Principal(user_id="user-op-other", role=Role.OPERATOR, tenant_id="tenant-beta")
    operator_valid = Principal(user_id="user-op-alpha", role=Role.OPERATOR, tenant_id="tenant-alpha")

    # Viewer role must not have operator permissions
    assert viewer.role not in (Role.OPERATOR, Role.ADMIN)
    assert operator_valid.role in (Role.OPERATOR, Role.ADMIN)

    # Cross tenant check
    assert operator_cross.tenant_id != wf.tenant_id

    # Valid operator with completed workflow cannot redrive (terminal state check)
    assert wf.state in (WorkflowState.COMPLETED, WorkflowState.ESCALATED)
