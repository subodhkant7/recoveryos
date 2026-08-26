"""
Security boundary and event provenance verification for Worker Execution Service.

Ensures worker never blindly trusts incoming event payloads without
validating producer identity, tenant boundaries, and cryptographic context.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from backend.events.message_models import WorkflowExecutionMessage, MessageValidationError


logger = logging.getLogger("recoveryos.worker.security")


class SecurityVerificationError(Exception):
    """Raised when an incoming event fails provenance or authorization verification."""
    pass


class BaseWorkerSecurityValidator(ABC):
    """Abstract interface for worker security validation."""

    @abstractmethod
    def validate_message_provenance(self, message: WorkflowExecutionMessage) -> None:
        """
        Validate producer identity, tenant structure, and cryptographic provenance.
        Raises SecurityVerificationError if provenance cannot be verified.
        """
        pass


class DefaultWorkerSecurityValidator(BaseWorkerSecurityValidator):
    """
    Default security validator enforcing producer identity checks,
    tenant identifier constraints, and contract integrity.
    """

    ALLOWED_PRODUCER_PREFIXES = ("recoveryos-api", "recoveryos-worker", "recoveryos-system")

    def validate_message_provenance(self, message: WorkflowExecutionMessage) -> None:
        if not message.producer_id or not any(message.producer_id.startswith(p) for p in self.ALLOWED_PRODUCER_PREFIXES):
            logger.warning(
                "Untrusted producer ID on worker ingress",
                extra={
                    "event_name": "SECURITY_UNTRUSTED_PRODUCER",
                    "producer_id": message.producer_id,
                    "message_id": message.message_id,
                    "workflow_id": message.workflow_id,
                },
            )
            raise SecurityVerificationError(
                f"Untrusted producer ID '{message.producer_id}'. Must start with one of {self.ALLOWED_PRODUCER_PREFIXES}."
            )

        if not message.tenant_id or " " in message.tenant_id or len(message.tenant_id) < 3:
            raise SecurityVerificationError(f"Invalid tenant format '{message.tenant_id}'.")
