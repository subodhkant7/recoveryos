"""
Domain event models for asynchronous workflow execution in RecoveryOS.

Provides strongly typed, validated event message schemas for Google Cloud Pub/Sub
dispatch and worker execution.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


SUPPORTED_SCHEMA_VERSIONS = {"1.0.0", "1.0"}


class WorkflowEventType(str, Enum):
    """Supported asynchronous workflow event types."""

    WORKFLOW_DISPATCH = "WORKFLOW_DISPATCH"
    STEP_EXECUTE = "STEP_EXECUTE"
    APPROVAL_RESUME = "APPROVAL_RESUME"
    RECOVERY_TRIGGER = "RECOVERY_TRIGGER"
    WORKFLOW_CANCEL = "WORKFLOW_CANCEL"


class MessageValidationError(ValueError):
    """Raised when an incoming Pub/Sub message violates contract validation rules."""
    pass


class WorkflowExecutionMessage(BaseModel):
    """
    Strongly validated message contract for asynchronous workflow dispatch.

    Carries workflow routing, tenant boundaries, OCC expected versions,
    correlation tracing IDs, and execution payloads.
    """

    schema_version: str = "1.0.0"
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: WorkflowEventType
    workflow_id: str
    tenant_id: str
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str
    expected_version: int = 1
    published_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    producer_id: str = "recoveryos-api"
    payload: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        if not v or v.strip() not in SUPPORTED_SCHEMA_VERSIONS:
            raise MessageValidationError(
                f"Unsupported schema version '{v}'. Supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        return v.strip()

    @field_validator("message_id", "workflow_id", "tenant_id", "idempotency_key", "producer_id")
    @classmethod
    def validate_non_empty_identifiers(cls, v: str, info) -> str:
        if not v or not isinstance(v, str) or not v.strip():
            raise MessageValidationError(f"Field '{info.field_name}' must be a non-empty string.")
        return v.strip()

    @field_validator("expected_version")
    @classmethod
    def validate_expected_version(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            raise MessageValidationError(f"Field 'expected_version' must be an integer >= 1 (got {v}).")
        return v

    @field_validator("published_at")
    @classmethod
    def validate_published_at(cls, v: datetime) -> datetime:
        now = datetime.now(timezone.utc)
        # Ensure timezone-aware UTC
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        # Clock skew tolerance: allow up to 10 minutes in future and 30 days in past
        if v > now + timedelta(minutes=10):
            raise MessageValidationError(f"Timestamp 'published_at' is in the future: {v}")
        if v < now - timedelta(days=30):
            raise MessageValidationError(f"Timestamp 'published_at' is too old (> 30 days): {v}")
        return v

    def to_pubsub_json(self) -> str:
        """Serialize message to deterministic JSON string for Pub/Sub payload."""
        return self.model_dump_json()

    def to_pubsub_attributes(self) -> dict[str, str]:
        """Extract Pub/Sub message attributes for subscription filter routing."""
        return {
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "event_type": self.event_type.value,
            "workflow_id": self.workflow_id,
            "tenant_id": self.tenant_id,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "expected_version": str(self.expected_version),
            "producer_id": self.producer_id,
        }

    @classmethod
    def from_pubsub_json(cls, raw_json: str | bytes) -> WorkflowExecutionMessage:
        """
        Deserialize and validate raw JSON into a WorkflowExecutionMessage.
        Raises MessageValidationError on malformed JSON or validation failure.
        """
        if not raw_json:
            raise MessageValidationError("Pub/Sub message payload is empty.")

        try:
            if isinstance(raw_json, bytes):
                raw_json = raw_json.decode("utf-8")
            data = json.loads(raw_json)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise MessageValidationError(f"Malformed JSON in message payload: {e}") from e

        if not isinstance(data, dict):
            raise MessageValidationError("Message payload root must be a JSON object.")

        try:
            return cls.model_validate(data)
        except Exception as e:
            raise MessageValidationError(f"Message contract validation failed: {e}") from e
