"""
Audit event models for RecoveryOS Operator Control Plane.

Defines the schema for structured, immutable audit log events.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field


class SecurityAuditEvent(BaseModel):
    """
    Immutable audit event record detailing who did what, to which workflow, and why.
    """

    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: str  # PRIVILEGED_MUTATION, RECOVERY_TRIGGERED, APPROVAL_DECIDED, CANCEL_TRIGGERED, AUTH_DENIAL
    actor_id: str = "anonymous"
    role: str = "none"
    tenant_id: str = "tenant-default"
    workflow_id: Optional[str] = None
    action: str = ""
    outcome: str = "DENIED"  # SUCCESS, DENIED, FAILED
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
