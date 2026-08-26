"""
Domain models and delivery status contracts for the Dedicated Worker Execution Service.

Defines delivery decisions (ACK, NACK, DEAD_LETTER), error classifications
(RETRYABLE, PERMANENT), and execution results.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class DeliveryStatus(str, Enum):
    """
    Asynchronous message processing outcome for message transport.
    """

    ACK = "ACK"                     # Successfully processed, skipped duplicate, or dropped terminal
    NACK = "NACK"                   # Transient/retryable failure; message should be redelivered
    DEAD_LETTER = "DEAD_LETTER"     # Permanent poison/malformed failure; message should route to DLQ


class FailureClassification(str, Enum):
    """Classification of processing failures."""

    RETRYABLE = "RETRYABLE"         # Transient persistence error, temporary OCC contention, network timeout
    PERMANENT = "PERMANENT"         # Malformed JSON, schema mismatch, tenant mismatch, security denial


class WorkerExecutionResult(BaseModel):
    """
    Structured outcome of worker task execution.
    """

    delivery_status: DeliveryStatus
    failure_type: FailureClassification | None = None
    workflow_id: str | None = None
    message_id: str | None = None
    tenant_id: str | None = None
    event_type: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
