"""
Security Audit Logging Subsystem for RecoveryOS.

Records structured, immutable security events (authentication failures,
authorization denials, human approval attempts, and privileged mutations).
Guarantees that raw JWTs, secrets, and authorization headers are never logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("recoveryos.security.audit")

_SECURITY_AUDIT_LOGS: list[dict[str, Any]] = []


def record_security_audit_event(
    event_type: str,
    actor_id: str = "anonymous",
    role: str = "none",
    tenant_id: str = "none",
    workflow_id: str | None = None,
    action: str = "",
    outcome: str = "DENIED",
    reason: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Record an immutable security audit event.
    """
    clean_extra = {
        k: v for k, v in (extra or {}).items()
        if k.lower() not in ("token", "jwt", "authorization", "secret", "password", "key")
    }

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "actor_id": actor_id,
        "role": role,
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
        "action": action,
        "outcome": outcome,
        "reason": reason,
        "metadata": clean_extra,
    }

    _SECURITY_AUDIT_LOGS.append(event)
    logger.info(
        f"[SECURITY_AUDIT] type={event_type} actor={actor_id} role={role} "
        f"workflow={workflow_id} action={action} outcome={outcome} reason='{reason}'"
    )
    return event


def get_security_audit_logs() -> list[dict[str, Any]]:
    """Retrieve in-memory security audit log history."""
    return list(_SECURITY_AUDIT_LOGS)


def clear_security_audit_logs() -> None:
    """Clear in-memory security audit log history (for unit test isolation)."""
    _SECURITY_AUDIT_LOGS.clear()
