"""
Dedicated Worker Execution Service for RecoveryOS.

Ingests workflow messages, enforces security boundaries, delegates to
WorkflowEventConsumer, and translates outcomes to ACK, NACK, and DEAD_LETTER decisions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.events.message_models import (
    WorkflowExecutionMessage,
    MessageValidationError,
)
from backend.events.consumer import (
    WorkflowEventConsumer,
    ConsumerExecutionError,
)
from backend.persistence.workflow_store import StaleWorkflowStateError
from backend.worker.models import (
    DeliveryStatus,
    FailureClassification,
    WorkerExecutionResult,
)
from backend.worker.security import (
    BaseWorkerSecurityValidator,
    DefaultWorkerSecurityValidator,
    SecurityVerificationError,
)
from backend.lifecycle import ShutdownManager, shutdown_manager as default_shutdown_manager
from backend.observability.logging import redact_sensitive_data


logger = logging.getLogger("recoveryos.worker.service")


class WorkflowWorkerService:
    """
    Dedicated Worker Service responsible for processing asynchronous messages.
    """

    def __init__(
        self,
        consumer: WorkflowEventConsumer,
        security_validator: BaseWorkerSecurityValidator | None = None,
        shutdown_manager: ShutdownManager | None = None,
        worker_id: str = "worker-default",
    ):
        self._consumer = consumer
        self._security_validator = security_validator or DefaultWorkerSecurityValidator()
        self._shutdown_manager = shutdown_manager or default_shutdown_manager
        self._worker_id = worker_id

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def process_raw_payload(self, raw_json: str | bytes) -> WorkerExecutionResult:
        """
        Process a raw JSON byte/string payload from message transport.
        Returns structured WorkerExecutionResult with delivery decision.
        """
        # 1. Check shutdown state
        if self._shutdown_manager and self._shutdown_manager.is_shutting_down:
            logger.warning("Worker is shutting down; rejecting new task with NACK for redelivery")
            return WorkerExecutionResult(
                delivery_status=DeliveryStatus.NACK,
                failure_type=FailureClassification.RETRYABLE,
                error_message="Worker is shutting down; message rejected for redelivery",
            )

        # 2. Parse and validate message contract
        try:
            message = WorkflowExecutionMessage.from_pubsub_json(raw_json)
        except MessageValidationError as e:
            redacted_err = str(redact_sensitive_data(str(e)))
            logger.error(
                "Malformed/Invalid message rejected as PERMANENT poison failure",
                extra={"event_name": "WORKER_POISON_MESSAGE", "error": redacted_err},
            )
            return WorkerExecutionResult(
                delivery_status=DeliveryStatus.DEAD_LETTER,
                failure_type=FailureClassification.PERMANENT,
                error_message=f"Message validation failed: {redacted_err}",
            )
        except Exception as e:
            redacted_err = str(redact_sensitive_data(str(e)))
            logger.error(
                "Unexpected payload parse failure",
                extra={"event_name": "WORKER_PARSE_FAILED", "error": redacted_err},
            )
            return WorkerExecutionResult(
                delivery_status=DeliveryStatus.DEAD_LETTER,
                failure_type=FailureClassification.PERMANENT,
                error_message=f"Unexpected parsing error: {redacted_err}",
            )

        return await self.process_message(message)

    async def process_message(self, message: WorkflowExecutionMessage) -> WorkerExecutionResult:
        """
        Execute a strongly validated WorkflowExecutionMessage.
        """
        # 1. Check shutdown state
        if self._shutdown_manager and self._shutdown_manager.is_shutting_down:
            return WorkerExecutionResult(
                delivery_status=DeliveryStatus.NACK,
                failure_type=FailureClassification.RETRYABLE,
                workflow_id=message.workflow_id,
                message_id=message.message_id,
                tenant_id=message.tenant_id,
                event_type=message.event_type.value,
                error_message="Worker is shutting down; message rejected for redelivery",
            )

        # 2. Validate event provenance & security context
        try:
            self._security_validator.validate_message_provenance(message)
        except SecurityVerificationError as e:
            logger.error(
                "Security provenance verification failed",
                extra={
                    "event_name": "WORKER_SECURITY_DENIED",
                    "workflow_id": message.workflow_id,
                    "message_id": message.message_id,
                    "error": str(e),
                },
            )
            return WorkerExecutionResult(
                delivery_status=DeliveryStatus.DEAD_LETTER,
                failure_type=FailureClassification.PERMANENT,
                workflow_id=message.workflow_id,
                message_id=message.message_id,
                tenant_id=message.tenant_id,
                event_type=message.event_type.value,
                error_message=f"Security verification failed: {e}",
            )

        # 3. Delegate to WorkflowEventConsumer
        try:
            outcome = await self._consumer.consume_message(message)
            status_val = outcome.get("status")

            if status_val == "PROCESSED":
                return WorkerExecutionResult(
                    delivery_status=DeliveryStatus.ACK,
                    workflow_id=message.workflow_id,
                    message_id=message.message_id,
                    tenant_id=message.tenant_id,
                    event_type=message.event_type.value,
                    details=outcome,
                )
            elif status_val in ("SKIPPED_DUPLICATE", "SKIPPED_TERMINAL"):
                return WorkerExecutionResult(
                    delivery_status=DeliveryStatus.ACK,
                    workflow_id=message.workflow_id,
                    message_id=message.message_id,
                    tenant_id=message.tenant_id,
                    event_type=message.event_type.value,
                    details=outcome,
                )
            else:
                return WorkerExecutionResult(
                    delivery_status=DeliveryStatus.ACK,
                    workflow_id=message.workflow_id,
                    message_id=message.message_id,
                    tenant_id=message.tenant_id,
                    event_type=message.event_type.value,
                    details=outcome,
                )

        except StaleWorkflowStateError as e:
            logger.warning(
                "OCC Version conflict during worker execution (Retryable)",
                extra={
                    "event_name": "WORKER_OCC_CONFLICT",
                    "workflow_id": message.workflow_id,
                    "message_id": message.message_id,
                    "error": str(e),
                },
            )
            return WorkerExecutionResult(
                delivery_status=DeliveryStatus.NACK,
                failure_type=FailureClassification.RETRYABLE,
                workflow_id=message.workflow_id,
                message_id=message.message_id,
                tenant_id=message.tenant_id,
                event_type=message.event_type.value,
                error_message=f"OCC conflict: {e}",
            )

        except ConsumerExecutionError as e:
            logger.error(
                "Permanent consumer execution error (e.g. tenant mismatch or missing workflow)",
                extra={
                    "event_name": "WORKER_CONSUMER_FATAL",
                    "workflow_id": message.workflow_id,
                    "message_id": message.message_id,
                    "error": str(e),
                },
            )
            return WorkerExecutionResult(
                delivery_status=DeliveryStatus.DEAD_LETTER,
                failure_type=FailureClassification.PERMANENT,
                workflow_id=message.workflow_id,
                message_id=message.message_id,
                tenant_id=message.tenant_id,
                event_type=message.event_type.value,
                error_message=str(e),
            )

        except Exception as e:
            logger.error(
                "Transient/Unexpected worker error during message processing",
                extra={
                    "event_name": "WORKER_TRANSIENT_ERROR",
                    "workflow_id": message.workflow_id,
                    "message_id": message.message_id,
                    "error": str(e),
                },
            )
            return WorkerExecutionResult(
                delivery_status=DeliveryStatus.NACK,
                failure_type=FailureClassification.RETRYABLE,
                workflow_id=message.workflow_id,
                message_id=message.message_id,
                tenant_id=message.tenant_id,
                event_type=message.event_type.value,
                error_message=f"Transient failure: {e}",
            )
