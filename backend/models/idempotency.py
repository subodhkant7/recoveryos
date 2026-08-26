"""
Idempotency and Distributed Operation Claim domain models.

Every externally mutating operation must be idempotent and coordinated.
- IdempotencyRecord: Tracks operation lifecycle and cached results.
- OperationClaim: Distributed lease and claim management across workers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class IdempotencyStatus(str, Enum):
    """Lifecycle status of an idempotent operation."""

    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class OperationStatus(str, Enum):
    """Lifecycle status of a distributed operation claim."""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STALE = "STALE"


class IdempotencyRecord(BaseModel):
    """
    Record of an operation for idempotency and crash reconciliation.

    Tracks whether an operation is in-flight, succeeded, or failed.
    """

    idempotency_key: str
    workflow_id: str
    tool_name: str
    target_entity_id: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: IdempotencyStatus = IdempotencyStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    step_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=24)
    )

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_completed(self) -> bool:
        return self.status == IdempotencyStatus.SUCCEEDED


class OperationClaim(BaseModel):
    """
    Durable operation claim model for distributed multi-worker coordination.
    """

    operation_id: str = Field(default_factory=lambda: f"op-{uuid.uuid4().hex[:12]}")
    idempotency_key: str
    workflow_id: str
    tool_name: str
    target_entity_id: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: OperationStatus = OperationStatus.CLAIMED
    owner_worker_id: str = "worker-default"
    lease_expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    version: int = 1

    @property
    def is_lease_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.lease_expires_at

    @property
    def is_completed(self) -> bool:
        return self.status == OperationStatus.COMPLETED
