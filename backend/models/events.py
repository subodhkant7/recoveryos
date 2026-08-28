"""
Workflow event model.

Immutable events that record everything that happens during
workflow execution. These form the audit trail and feed the
frontend timeline view.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Canonical UTC datetime helper."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Canonical UTC ISO-8601 formatted string helper."""
    return datetime.now(timezone.utc).isoformat()


class EventType(str, Enum):
    """Classification of workflow events."""

    STATE_CHANGE = "STATE_CHANGE"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    FAILURE_DETECTED = "FAILURE_DETECTED"
    DIAGNOSIS_COMPLETE = "DIAGNOSIS_COMPLETE"
    RECOVERY_PLAN_PROPOSED = "RECOVERY_PLAN_PROPOSED"
    POLICY_DECISION = "POLICY_DECISION"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    VERIFICATION_RESULT = "VERIFICATION_RESULT"
    EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
    WORKFLOW_RESUMED = "WORKFLOW_RESUMED"
    AGENT_REASONING = "AGENT_REASONING"


class WorkflowEvent(BaseModel):
    """
    An immutable event in the workflow's history.

    Events are append-only. They are never updated or deleted.
    They form the complete audit trail and power the frontend timeline.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    event_type: EventType
    title: str = ""
    detail: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = ""  # "taskmaster" | "recovery_specialist" | "policy_engine" | "human" | "system"
    timestamp: datetime = Field(default_factory=utc_now)
    occurred_at: datetime = Field(default_factory=utc_now)
