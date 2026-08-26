"""
Evidence and verification domain models.

Evidence is any structured data produced during workflow execution
that can be used for reasoning, policy decisions, or auditing.
Verification results are a specific kind of evidence that independently
confirm whether an outcome was achieved.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvidenceType(str, Enum):
    """Classification of evidence by its source."""

    TOOL_RESULT = "TOOL_RESULT"
    VERIFICATION = "VERIFICATION"
    DIAGNOSIS = "DIAGNOSIS"
    HUMAN_INPUT = "HUMAN_INPUT"
    SERVICE_STATUS = "SERVICE_STATUS"
    FAILURE_DETAIL = "FAILURE_DETAIL"


class Evidence(BaseModel):
    """
    A piece of evidence collected during workflow execution.

    Evidence is immutable once created. If new evidence supersedes
    old evidence, the `supersedes` field links them.
    """

    evidence_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    step_id: str | None = None
    source: str  # Tool name or agent name that produced it
    evidence_type: EvidenceType
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    supersedes: str | None = None  # ID of evidence this replaces


class VerificationResult(BaseModel):
    """
    Independent verification of whether an outcome was achieved.

    This is NOT "the tool said it worked." This is a separate query
    to the target system to confirm the outcome independently.
    """

    verification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    target: str  # What was verified — step_id or outcome_id
    target_type: str = "outcome"  # "step" or "outcome"
    method: str  # Description of how verification was performed
    passed: bool
    evidence: dict[str, Any] = Field(default_factory=dict)
    discrepancies: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
