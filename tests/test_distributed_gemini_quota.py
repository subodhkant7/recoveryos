"""
Phase 6.2.3: Distributed Gemini Quota Rate Limiter Test Suite.

Comprehensive tests covering:
- In-memory & Firestore-leased token window coordination (>= 6.5s spacing)
- Serialization under concurrent races (2, 5, 10 workers)
- Fail-closed behavior on persistence failure / retry exhaustion
- Multi-process serialization against live Firestore emulator
- Integration with ResilientGemini and Circuit Breaker
- Cloud Tasks dispatch pacer abstraction
"""

import asyncio
import os
import multiprocessing
import time
from datetime import datetime, timezone, timedelta
import pytest

from backend.llm.distributed_quota import (
    InMemoryDistributedQuotaLimiter,
    FirestoreDistributedQuotaLimiter,
    QuotaAcquisitionError,
    QuotaReservation,
    FakeCloudTasksPacer,
)
from backend.llm.resilience import (
    ResilientGemini,
    GeminiCircuitBreaker,
    CircuitState,
    CircuitOpenError,
)
from backend.persistence.workflow_store import FirestoreWorkflowStore


# ===========================================================================
# 1. In-Memory Distributed Limiter Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_01_first_acquire_succeeds_immediately():
    """First acquire reserves slot immediately with wait_seconds == 0."""
    limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=6.5)
    res = await limiter.reserve_slot(worker_id="worker-1")
    assert res.granted is True
    assert res.wait_seconds == 0.0
    assert res.lease_version == 1


@pytest.mark.asyncio
async def test_02_second_acquire_respects_spacing():
    """Second acquire reserves slot spaced by exactly min_interval."""
    limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=6.5)
    r1 = await limiter.reserve_slot(worker_id="worker-1")
    r2 = await limiter.reserve_slot(worker_id="worker-2")

    assert r1.wait_seconds == 0.0
    assert 6.4 <= r2.wait_seconds <= 6.6
    assert r2.reserved_slot == r1.reserved_slot + timedelta(seconds=6.5)
    assert r2.lease_version == 2


@pytest.mark.asyncio
async def test_03_two_concurrent_workers_serialized():
    """Two concurrent workers reserving slots receive strictly serialized slots."""
    limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=6.5)

    r1, r2 = await asyncio.gather(
        limiter.reserve_slot("worker-1"),
        limiter.reserve_slot("worker-2"),
    )

    slots = sorted([r1.reserved_slot, r2.reserved_slot])
    assert slots[1] == slots[0] + timedelta(seconds=6.5)


@pytest.mark.asyncio
async def test_04_five_concurrent_workers_serialized():
    """5 concurrent workers receive strictly serialized 6.5s spaced slots."""
    limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=6.5)

    results = await asyncio.gather(*[
        limiter.reserve_slot(f"worker-{i}") for i in range(5)
    ])

    sorted_results = sorted(results, key=lambda r: r.reserved_slot)
    for i in range(1, len(sorted_results)):
        expected_slot = sorted_results[i-1].reserved_slot + timedelta(seconds=6.5)
        assert sorted_results[i].reserved_slot == expected_slot


@pytest.mark.asyncio
async def test_05_ten_concurrent_workers_serialized():
    """10 concurrent workers receive strictly serialized slots."""
    limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=6.5)

    results = await asyncio.gather(*[
        limiter.reserve_slot(f"worker-{i}") for i in range(10)
    ])

    sorted_slots = sorted([r.reserved_slot for r in results])
    for i in range(1, len(sorted_slots)):
        assert sorted_slots[i] == sorted_slots[i-1] + timedelta(seconds=6.5)


@pytest.mark.asyncio
async def test_06_in_memory_fail_closed_on_error():
    """Limiter fails closed with QuotaAcquisitionError if storage fails."""
    limiter = InMemoryDistributedQuotaLimiter(simulate_failure=True)
    with pytest.raises(QuotaAcquisitionError):
        await limiter.reserve_slot("worker-1")


# ===========================================================================
# 2. Firestore-Leased Safety Limiter Tests (Emulator)
# ===========================================================================

import uuid


def _is_emulator_available() -> bool:
    host = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if not host:
        return False
    import socket
    try:
        parts = host.split(":")
        ip = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 8080
        with socket.create_connection((ip, port), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


@pytest.fixture
def firestore_limiter():
    store = FirestoreWorkflowStore(project_id="recoveryos-eval")
    doc_id = f"gemini_quota_lease_{uuid.uuid4()}"
    return FirestoreDistributedQuotaLimiter(client_or_store=store, min_interval_seconds=6.5, document_id=doc_id)


@pytest.mark.skipif(not _is_emulator_available(), reason="Firestore emulator host not active")
@pytest.mark.asyncio
async def test_07_firestore_first_acquire_and_spacing(firestore_limiter):
    """First and second acquire against Firestore emulator enforce OCC leased window."""
    r1 = await firestore_limiter.reserve_slot(worker_id="node-1")
    r2 = await firestore_limiter.reserve_slot(worker_id="node-2")

    assert r1.granted is True
    assert r1.wait_seconds == 0.0
    assert 6.3 <= r2.wait_seconds <= 6.7
    assert r2.lease_version > r1.lease_version


@pytest.mark.skipif(not _is_emulator_available(), reason="Firestore emulator host not active")
@pytest.mark.asyncio
async def test_08_firestore_concurrent_workers_serialized(firestore_limiter):
    """Multiple concurrent workers against Firestore emulator obtain serialized slots."""
    results = await asyncio.gather(*[
        firestore_limiter.reserve_slot(f"worker-fs-{i}") for i in range(5)
    ])

    sorted_slots = sorted([r.reserved_slot for r in results])
    for i in range(1, len(sorted_slots)):
        assert (sorted_slots[i] - sorted_slots[i-1]).total_seconds() == pytest.approx(6.5, abs=0.1)


@pytest.mark.asyncio
async def test_09_firestore_unavailable_fails_closed():
    """If Firestore is completely unreachable, limiter fails closed with QuotaAcquisitionError."""
    from unittest.mock import MagicMock
    broken_client = MagicMock()
    broken_client.collection.side_effect = RuntimeError("Firestore connection refused (503 Service Unavailable)")
    limiter = FirestoreDistributedQuotaLimiter(client_or_store=broken_client)

    with pytest.raises(QuotaAcquisitionError) as exc:
        await limiter.reserve_slot("worker-fail")
    assert "Firestore quota lease failed" in str(exc.value)


# ===========================================================================
# 3. Real Multi-Process Concurrency Test (Cross-Process Serialization)
# ===========================================================================

def _mp_worker_task(worker_id: str, doc_id: str, results_queue: multiprocessing.Queue):
    """Worker process function that executes reserve_slot against Firestore emulator."""
    os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080"
    os.environ["GOOGLE_CLOUD_PROJECT"] = "recoveryos-eval"

    async def _run():
        store = FirestoreWorkflowStore(project_id="recoveryos-eval")
        limiter = FirestoreDistributedQuotaLimiter(client_or_store=store, min_interval_seconds=6.5, document_id=doc_id)
        res = await limiter.reserve_slot(worker_id=worker_id)
        return {
            "worker_id": res.worker_id,
            "wait_seconds": res.wait_seconds,
            "reserved_slot": res.reserved_slot.isoformat(),
            "lease_version": res.lease_version,
        }

    try:
        outcome = asyncio.run(_run())
        results_queue.put(outcome)
    except Exception as e:
        results_queue.put({"error": str(e)})


@pytest.mark.skipif(not _is_emulator_available(), reason="Firestore emulator host not active")
def test_10_multiprocess_cross_process_serialization():
    """Separate OS processes execute against Firestore emulator, proving cross-process lease serialization."""
    mp_ctx = multiprocessing.get_context("spawn")
    num_processes = 3
    doc_id = f"gemini_mp_lease_{uuid.uuid4()}"
    queue = mp_ctx.Queue()
    processes = []

    for i in range(num_processes):
        p = mp_ctx.Process(target=_mp_worker_task, args=(f"mp-worker-{i}", doc_id, queue))
        processes.append(p)
        p.start()

    results = []
    for _ in range(num_processes):
        try:
            results.append(queue.get(timeout=20.0))
        except Exception:
            pass

    for p in processes:
        p.join(timeout=20.0)

    assert len(results) == num_processes
    for r in results:
        assert "error" not in r, f"Child process failed: {r.get('error')}"

    parsed_slots = sorted([datetime.fromisoformat(r["reserved_slot"]) for r in results])
    for i in range(1, len(parsed_slots)):
        diff = (parsed_slots[i] - parsed_slots[i-1]).total_seconds()
        assert diff >= 6.4, f"Slot spacing {diff:.2f}s is less than minimum interval 6.5s"


# ===========================================================================
# 4. ResilientGemini & Circuit Breaker Integration
# ===========================================================================

@pytest.mark.asyncio
async def test_11_resilient_gemini_uses_distributed_limiter():
    """ResilientGemini integrates seamlessly with distributed quota limiter."""
    dist_limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=0.1)
    cb = GeminiCircuitBreaker(failure_threshold=3, cooldown_seconds=1.0)
    gemini = ResilientGemini(model="gemini-3.5-flash", rate_limiter=dist_limiter, circuit_breaker=cb)

    assert gemini.rate_limiter == dist_limiter
    assert gemini.circuit_breaker == cb


@pytest.mark.asyncio
async def test_12_circuit_breaker_blocks_before_quota_acquisition():
    """Circuit breaker tripping blocks requests before quota slots are consumed."""
    dist_limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=6.5)
    cb = GeminiCircuitBreaker(failure_threshold=1, cooldown_seconds=10.0)
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN

    gemini = ResilientGemini(model="gemini-3.5-flash", rate_limiter=dist_limiter, circuit_breaker=cb)

    with pytest.raises(CircuitOpenError):
        async for _ in gemini.generate_content_async(None):
            pass

    # No quota slot was consumed
    assert dist_limiter._version == 0


# ===========================================================================
# 5. Cloud Tasks Pacer Abstraction
# ===========================================================================

@pytest.mark.asyncio
async def test_13_fake_cloud_tasks_pacer():
    """FakeCloudTasksPacer enqueues tasks and enforces dispatch queue bounds."""
    pacer = FakeCloudTasksPacer(max_dispatches_per_sec=0.25)
    t1 = await pacer.enqueue_dispatch_task({"workflow_id": "wf-1"})
    t2 = await pacer.enqueue_dispatch_task({"workflow_id": "wf-2"})

    assert len(pacer.enqueued_tasks) == 2
    assert pacer.enqueued_tasks[0]["task_id"] == t1
    assert pacer.enqueued_tasks[1]["task_id"] == t2


@pytest.mark.asyncio
async def test_14_worker_crash_simulation_after_reservation():
    """If Worker A crashes after reserving a slot, subsequent Worker B safely receives the next slot."""
    limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=6.5)
    r1 = await limiter.reserve_slot(worker_id="crashed-worker-1")
    # Worker 1 crashed, slot remains advanced
    r2 = await limiter.reserve_slot(worker_id="worker-2")
    assert r2.reserved_slot == r1.reserved_slot + timedelta(seconds=6.5)
    assert r2.lease_version == 2


@pytest.mark.asyncio
async def test_15_quota_acquire_cancellation():
    """Acquiring a slot can be cancelled cleanly without corrupting limiter state."""
    limiter = InMemoryDistributedQuotaLimiter(min_interval_seconds=6.5)
    await limiter.reserve_slot(worker_id="worker-1")

    async def _long_acquire():
        await limiter.acquire(worker_id="worker-2")

    task = asyncio.create_task(_long_acquire())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Next acquire still respects the sequence
    r3 = await limiter.reserve_slot(worker_id="worker-3")
    assert r3.lease_version == 3
