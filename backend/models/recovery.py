"""
Recovery domain models.

Defines Failure, RecoveryPlan, RecoveryStep, and related types.

A RecoveryPlan is a structured, reasoned candidate path toward the
original outcome, including diagnosis, proposed steps, expected evidence,
evidence references, and rollback considerations.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FailureType(str, Enum):
    """Classification of failure by its nature."""

    TOOL_ERROR = "TOOL_ERROR"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    TIMEOUT = "TIMEOUT"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    DATA_QUALITY = "DATA_QUALITY"


class RecoveryPlanStatus(str, Enum):
    """Lifecycle status of a recovery plan."""

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class Confidence(str, Enum):
    """Agent confidence in a recovery plan."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Failure(BaseModel):
    """
    A recorded failure during workflow execution.

    Created when a step fails or verification detects a problem.
    The Recovery Specialist reads failures to diagnose and plan recovery.
    """

    failure_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    step_id: str
    error_type: FailureType
    error_detail: str
    raw_error: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    diagnosed: bool = False
    diagnosis: str | None = None
    root_cause: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RecoveryStep(BaseModel):
    """
    A single step in a recovery plan.

    Each step maps to a tool call with a rationale explaining
    why this specific tool/args were chosen, and what evidence
    the agent expects the step to produce.
    """

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""  # Why THIS step was chosen
    expected_result: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class RecoveryPlan(BaseModel):
    """
    A structured candidate path toward the original outcome.

    Produced dynamically by the Recovery Specialist based on diagnostic
    evidence, discovered capabilities, and the OutcomeContract.
    """

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    target_outcome_id: str
    failure_id: str | None = None

    # Diagnosis & Objectives
    diagnosis: str
    root_cause: str = ""
    objective: str = ""
    constraints: list[str] = Field(default_factory=list)

    # Proposed Path
    proposed_steps: list[RecoveryStep] = Field(default_factory=list)

    # Evidence & Verification
    evidence_ids: list[str] = Field(default_factory=list)
    expected_evidence: dict[str, Any] = Field(default_factory=dict)
    verification_strategy: str = ""

    # Reasoning, Risks, and Confidence
    reasoning: str = ""
    risks: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    rollback_notes: str = ""

    # Lifecycle & Audit
    status: RecoveryPlanStatus = RecoveryPlanStatus.PROPOSED
    policy_decision_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
