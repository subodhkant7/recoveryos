"""
Human approval domain models.

HumanApproval represents a real persisted workflow state where
execution is paused pending human decision. This is not a UI
illusion — the workflow genuinely hibernates.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ApprovalStatus(str, Enum):
    """Status of a human approval request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class HumanApproval(BaseModel):
    """
    A request for human approval.

    Created when the policy engine determines that evidence is
    contradictory or an action exceeds automated authority.
    The workflow is paused until this is resolved via API call.
    """

    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    action_tool: str = ""
    action_args: dict[str, Any] = Field(default_factory=dict)
    policy_rule: str = ""
    reason: str = ""
    plan_id: str | None = None
    step_id: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    title: str = ""
    description: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    options: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    dedup_key: str = ""
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    selected_option: dict[str, Any] | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == ApprovalStatus.APPROVED

    @property
    def is_rejected(self) -> bool:
        return self.status == ApprovalStatus.REJECTED
