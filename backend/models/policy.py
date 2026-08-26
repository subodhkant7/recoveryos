"""
Policy domain models.

PolicyDecision and PolicyRuleResult are the output of the
deterministic policy engine. These are never LLM decisions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class PolicyOutcome(str, Enum):
    """Result of a policy evaluation."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REQUIRES_HUMAN_APPROVAL = "REQUIRES_HUMAN_APPROVAL"


class PolicyRuleResult(BaseModel):
    """Result of evaluating a single policy rule."""

    rule_name: str
    passed: bool
    detail: str


class PolicyDecision(BaseModel):
    """
    The deterministic output of the policy engine.

    Records which rules were evaluated, whether each passed,
    and the final outcome. This is auditable and reproducible.
    """

    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    plan_id: str | None = None
    tool_name: str | None = None
    rules_evaluated: list[PolicyRuleResult] = Field(default_factory=list)
    outcome: PolicyOutcome
    reason: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
