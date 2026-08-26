"""
Distributed Gemini Quota Rate Limiting & Cloud Tasks Coordination Subsystem.

Enforces distributed rate limiting across multi-worker Cloud Run fleets:
1. Primary Tier: Cloud Tasks Queue dispatch rate limiting (~0.25 dispatches/sec -> <= 15 RPM).
2. Safety Tier: Firestore Transactional Leased-Window on `/system/gemini_quota_lease` (>= 6.5s spacing).
3. Fail-closed safety on database failure or contention exhaustion.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Any
from pydantic import BaseModel, Field

from backend.config import config
from backend.observability.logging import redact_sensitive_data


logger = logging.getLogger("recoveryos.llm.distributed_quota")


class QuotaAcquisitionError(Exception):
    """Raised when distributed quota acquisition fails closed."""
    pass


class QuotaReservation(BaseModel):
    """
    Structured outcome of a distributed quota lease reservation.
    """

    granted: bool
    wait_seconds: float = 0.0
    reserved_slot: datetime
    worker_id: str
    tier: str = "FIRESTORE_LEASED"
    lease_version: int = 1


class BaseDistributedQuotaLimiter(ABC):
    """Abstract interface for distributed rate limiting across worker fleets."""

    @abstractmethod
    async def acquire(self, worker_id: str = "worker-default") -> float:
        """
        Acquires permission to execute a Gemini call.
        Returns the duration in seconds waited to satisfy the distributed lease.
        """
        pass

    @abstractmethod
    async def reserve_slot(self, worker_id: str = "worker-default") -> QuotaReservation:
        """
        Atomically reserves the next available time slot without sleeping.
        """
        pass


class InMemoryDistributedQuotaLimiter(BaseDistributedQuotaLimiter):
    """
    Thread-safe in-memory distributed quota limiter for unit tests and local simulation.
    """

    def __init__(
        self,
        min_interval_seconds: float = 6.5,
        simulate_failure: bool = False,
    ):
        self._min_interval = min_interval_seconds
        self._next_allowed_at: datetime | None = None
        self._version: int = 0
        self._lock = asyncio.Lock()
        self._simulate_failure = simulate_failure

    def set_simulate_failure(self, fail: bool) -> None:
        self._simulate_failure = fail

    async def reserve_slot(self, worker_id: str = "worker-default") -> QuotaReservation:
        if self._simulate_failure:
            raise QuotaAcquisitionError("Simulated distributed quota store failure (Fail-closed)")

        async with self._lock:
            now = datetime.now(timezone.utc)
            self._version += 1

            if self._next_allowed_at is None or now >= self._next_allowed_at:
                slot = now
                self._next_allowed_at = now + timedelta(seconds=self._min_interval)
                wait_seconds = 0.0
            else:
                slot = self._next_allowed_at
                self._next_allowed_at = slot + timedelta(seconds=self._min_interval)
                wait_seconds = (slot - now).total_seconds()

            logger.info(
                "Distributed quota slot reserved in-memory",
                extra={
                    "event_name": "QUOTA_SLOT_RESERVED",
                    "worker_id": worker_id,
                    "wait_seconds": wait_seconds,
                    "lease_version": self._version,
                },
            )

            return QuotaReservation(
                granted=True,
                wait_seconds=max(0.0, wait_seconds),
                reserved_slot=slot,
                worker_id=worker_id,
                tier="IN_MEMORY_LEASED",
                lease_version=self._version,
            )

    async def acquire(self, worker_id: str = "worker-default") -> float:
        reservation = await self.reserve_slot(worker_id=worker_id)
        if reservation.wait_seconds > 0:
            logger.info(
                f"Distributed quota pacing: waiting {reservation.wait_seconds:.2f}s",
                extra={
                    "event_name": "QUOTA_PACING_SLEEP",
                    "worker_id": worker_id,
                    "wait_seconds": reservation.wait_seconds,
                },
            )
            await asyncio.sleep(reservation.wait_seconds)
        return reservation.wait_seconds


class FirestoreDistributedQuotaLimiter(BaseDistributedQuotaLimiter):
    """
    Production Safety-Tier: Coordinates rate limiting using atomic Firestore OCC transactions
    on the `/system/gemini_quota_lease` document.
    """

    def __init__(
        self,
        client_or_store: Any = None,
        min_interval_seconds: float = 6.5,
        document_id: str = "gemini_quota_lease",
    ):
        self._store = client_or_store
        self._min_interval = min_interval_seconds
        self._document_id = document_id
        self._cached_client = None
        self._process_lock = asyncio.Lock()

    async def _get_client(self):
        if self._cached_client is not None:
            return self._cached_client

        if hasattr(self._store, "_get_client"):
            self._cached_client = await self._store._get_client()
            return self._cached_client

        if hasattr(self._store, "collection"):
            self._cached_client = self._store
            return self._cached_client

        from google.cloud import firestore
        kwargs: dict[str, Any] = {"project": config.google_cloud_project}
        if config.firestore_database and config.firestore_database != "(default)":
            kwargs["database"] = config.firestore_database
        self._cached_client = firestore.AsyncClient(**kwargs)
        return self._cached_client

    async def reserve_slot(self, worker_id: str = "worker-default") -> QuotaReservation:
        """
        Atomically reserve next allowed Gemini execution slot via Firestore transaction.
        Never holds transaction open while sleeping.
        """
        async with self._process_lock:
            try:
                client = await self._get_client()
                from google.cloud import firestore
                doc_ref = client.collection("system").document(self._document_id)

                @firestore.async_transactional
                async def _reserve_tx(transaction):
                    now = datetime.now(timezone.utc)
                    doc = await doc_ref.get(transaction=transaction)

                    if not doc.exists:
                        slot = now
                        next_allowed = now + timedelta(seconds=self._min_interval)
                        wait_seconds = 0.0
                        version = 1
                        data = {
                            "next_allowed_at": next_allowed.isoformat(),
                            "last_reserved_by": worker_id,
                            "updated_at": now.isoformat(),
                            "version": version,
                        }
                        transaction.set(doc_ref, data)
                        return True, wait_seconds, slot, version

                    data = doc.to_dict() or {}
                    next_str = data.get("next_allowed_at")
                    version = data.get("version", 1) + 1

                    try:
                        next_allowed_dt = datetime.fromisoformat(next_str) if next_str else now
                    except ValueError:
                        next_allowed_dt = now

                    if now >= next_allowed_dt:
                        slot = now
                        next_allowed = now + timedelta(seconds=self._min_interval)
                        wait_seconds = 0.0
                    else:
                        slot = next_allowed_dt
                        next_allowed = slot + timedelta(seconds=self._min_interval)
                        wait_seconds = (slot - now).total_seconds()

                    update_payload = {
                        "next_allowed_at": next_allowed.isoformat(),
                        "last_reserved_by": worker_id,
                        "updated_at": now.isoformat(),
                        "version": version,
                    }
                    transaction.set(doc_ref, update_payload)
                    return True, wait_seconds, slot, version

                tx = client.transaction(max_attempts=15)
                granted, wait_sec, slot_dt, ver = await _reserve_tx(tx)

                logger.info(
                    "Firestore distributed quota slot acquired",
                    extra={
                        "event_name": "FIRESTORE_QUOTA_RESERVED",
                        "worker_id": worker_id,
                        "wait_seconds": wait_sec,
                        "lease_version": ver,
                    },
                )

                return QuotaReservation(
                    granted=granted,
                    wait_seconds=max(0.0, wait_sec),
                    reserved_slot=slot_dt,
                    worker_id=worker_id,
                    tier="FIRESTORE_LEASED",
                    lease_version=ver,
                )

            except Exception as e:
                logger.error(
                    f"Firestore quota lease failure (Fail-closed): {e}",
                    extra={
                        "event_name": "FIRESTORE_QUOTA_FAIL_CLOSED",
                        "worker_id": worker_id,
                        "error": str(redact_sensitive_data(str(e))),
                    },
                )
                raise QuotaAcquisitionError(f"Firestore quota lease failed: {e}") from e

    async def acquire(self, worker_id: str = "worker-default") -> float:
        reservation = await self.reserve_slot(worker_id=worker_id)
        if reservation.wait_seconds > 0:
            logger.info(
                f"Distributed quota pacing: waiting {reservation.wait_seconds:.2f}s",
                extra={
                    "event_name": "FIRESTORE_QUOTA_SLEEP",
                    "worker_id": worker_id,
                    "wait_seconds": reservation.wait_seconds,
                },
            )
            await asyncio.sleep(reservation.wait_seconds)
        return reservation.wait_seconds


# ---------------------------------------------------------------------------
# Cloud Tasks Pacing Abstraction (Primary Tier)
# ---------------------------------------------------------------------------

class BaseCloudTasksPacer(ABC):
    """Abstract interface for Cloud Tasks dispatch rate control."""

    @abstractmethod
    async def enqueue_dispatch_task(self, task_payload: dict[str, Any], queue_name: str = "recoveryos-gemini-queue") -> str:
        """Enqueue task for paced dispatch."""
        pass


class FakeCloudTasksPacer(BaseCloudTasksPacer):
    """Local simulation of Cloud Tasks queue with max 0.25 dispatches/sec bound."""

    def __init__(self, max_dispatches_per_sec: float = 0.25):
        self.max_dispatches_per_sec = max_dispatches_per_sec
        self.enqueued_tasks: list[dict[str, Any]] = []

    async def enqueue_dispatch_task(self, task_payload: dict[str, Any], queue_name: str = "recoveryos-gemini-queue") -> str:
        task_id = f"task-{len(self.enqueued_tasks) + 1}"
        self.enqueued_tasks.append({"task_id": task_id, "queue": queue_name, "payload": task_payload})
        return task_id


class GcpCloudTasksPacer(BaseCloudTasksPacer):
    """GCP Cloud Tasks client boundary for future production deployment."""

    def __init__(self, project_id: str | None = None, location: str = "asia-east1"):
        self.project_id = project_id or config.google_cloud_project
        self.location = location

    async def enqueue_dispatch_task(self, task_payload: dict[str, Any], queue_name: str = "recoveryos-gemini-queue") -> str:
        raise NotImplementedError("GCP Cloud Tasks live client is provisioned in Phase 6.2.4")
