"""
Workflow domain models.

Defines the core workflow lifecycle: Workflow, WorkflowStep, WorkflowState,
OutcomeContract, RequiredOutcome, and Constraint.

Architecture note: The workflow state machine tracks LIFECYCLE PHASES,
not individual step progress. Step-level state lives on WorkflowStep.status.
This keeps the agent in control of step ordering and selection rather
than having a rigid state machine drive execution.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WorkflowState(str, Enum):
    """
    Workflow lifecycle phases.

    These represent the PHASE the workflow is in, not the status
    of any individual step. The agent decides which steps to run
    and in what order. The workflow engine only constrains which
    phase transitions are valid.
    """

    CREATED = "CREATED"
    EXECUTING = "EXECUTING"
    RECOVERING = "RECOVERING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    VERIFYING = "VERIFYING"
    UNKNOWN = "UNKNOWN"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"


# Valid phase transitions — the workflow engine enforces these.
# The agent drives step selection within a phase.
VALID_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.CREATED: {WorkflowState.EXECUTING},
    WorkflowState.EXECUTING: {
        WorkflowState.VERIFYING,          # Agent believes all steps are done
        WorkflowState.RECOVERING,         # Agent detects failure
        WorkflowState.AWAITING_APPROVAL,  # Policy requires human decision
        WorkflowState.ESCALATED,          # Unrecoverable without help
        WorkflowState.UNKNOWN,             # Process crash or interrupted execution
    },
    WorkflowState.RECOVERING: {
        WorkflowState.EXECUTING,           # Recovery plan approved, resume
        WorkflowState.AWAITING_APPROVAL,   # Policy requires human decision
        WorkflowState.ESCALATED,           # Policy rejected / budget exceeded
        WorkflowState.UNKNOWN,             # Interrupted recovery
    },
    WorkflowState.AWAITING_APPROVAL: {
        WorkflowState.EXECUTING,    # Human approved
        WorkflowState.ESCALATED,    # Human rejected
        WorkflowState.UNKNOWN,      # Interrupted approval wait
    },
    WorkflowState.UNKNOWN: {
        WorkflowState.EXECUTING,    # Reconciled and resumed
        WorkflowState.RECOVERING,   # Reconciled with failure requiring recovery
        WorkflowState.ESCALATED,    # Reconciliation confirmed unrecoverable
    },
    WorkflowState.VERIFYING: {
        WorkflowState.COMPLETED,    # All outcomes independently verified
        WorkflowState.RECOVERING,   # Verification found problems
        WorkflowState.UNKNOWN,      # Interrupted verification
    },
    WorkflowState.COMPLETED: set(),   # Terminal
    WorkflowState.ESCALATED: set(),   # Terminal
}


class StepStatus(str, Enum):
    """Status of an individual workflow step."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# OutcomeContract — what the agent must achieve
# ---------------------------------------------------------------------------


class Constraint(BaseModel):
    """
    A rule the agent must follow while achieving outcomes.

    Constraints are enforced by the policy engine (deterministic code),
    not by the agent (LLM reasoning).
    """

    constraint_id: str
    description: str
    enforcement: str = "policy"  # "policy" or "verification"


class RequiredOutcome(BaseModel):
    """
    A single business outcome that must be achieved.

    Includes acceptance criteria and verification requirements so the
    agent knows what "done" looks like and the verification tool knows
    how to independently confirm it.
    """

    outcome_id: str
    description: str
    acceptance_criteria: dict[str, Any] = Field(default_factory=dict)
    verification_method: str = ""
    required_evidence: list[str] = Field(default_factory=list)
    verified: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class OutcomeContract(BaseModel):
    """
    The business outcome contract for a workflow.

    This is what the agent optimizes toward. The agent is free to
    choose any path that achieves the required outcomes while
    respecting constraints and avoiding prohibited outcomes.

    The system does not consider a workflow complete until every
    required outcome has been independently verified.
    """

    workflow_id: str

    required_outcomes: list[RequiredOutcome]
    # Each outcome specifies WHAT must be true + HOW to verify it

    constraints: list[Constraint] = Field(default_factory=list)
    # Rules the agent must follow (enforced by policy engine)

    prohibited_outcomes: list[str] = Field(default_factory=list)
    # Things that must NOT happen (e.g., "double_charge")

    def all_verified(self) -> bool:
        """Check if every required outcome has been verified."""
        return all(o.verified for o in self.required_outcomes)

    def unverified_outcomes(self) -> list[RequiredOutcome]:
        """Return outcomes that have not yet been verified."""
        return [o for o in self.required_outcomes if not o.verified]

    def get_outcome(self, outcome_id: str) -> RequiredOutcome | None:
        """Look up a specific outcome by ID."""
        return next(
            (o for o in self.required_outcomes if o.outcome_id == outcome_id),
            None,
        )


# ---------------------------------------------------------------------------
# WorkflowStep — an individual tool invocation
# ---------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    """
    A single step in a workflow.

    Each step maps to a tool invocation. Steps track their own
    status, result, evidence, and idempotency independently.
    The agent decides which steps to create and in what order.
    """

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    name: str
    description: str = ""
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    evidence_id: str | None = None
    idempotency_key: str = ""
    attempt_count: int = 0
    max_attempts: int = 3
    is_recovery_step: bool = False
    recovery_plan_id: str | None = None
    target_outcome_id: str | None = None  # Which outcome this step works toward
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.idempotency_key:
            self.idempotency_key = (
                f"{self.workflow_id}:{self.step_id}:{self.attempt_count}"
            )


# ---------------------------------------------------------------------------
# Workflow — the top-level workflow instance
# ---------------------------------------------------------------------------


class Workflow(BaseModel):
    """
    A workflow instance.

    Represents a single execution of a business process (e.g., customer
    onboarding). Owns an OutcomeContract and a list of steps. The
    workflow state tracks which lifecycle phase the system is in; the
    agent decides what happens within each phase.
    """

    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = "tenant-default"
    name: str = ""
    scenario: str = ""
    customer_data: dict[str, Any] = Field(default_factory=dict)
    contract: OutcomeContract | None = None
    state: WorkflowState = WorkflowState.CREATED
    version: int = 1
    current_step_id: str | None = None
    recovery_attempts: int = 0
    max_recovery_attempts: int = 3
    idempotency_key: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resumed_at: datetime | None = None
    completed_at: datetime | None = None

    def can_transition_to(self, new_state: WorkflowState) -> bool:
        """Check if a state transition is valid."""
        return new_state in VALID_TRANSITIONS.get(self.state, set())

    @property
    def is_terminal(self) -> bool:
        """Check if the workflow is in a terminal state."""
        return self.state in (WorkflowState.COMPLETED, WorkflowState.ESCALATED)

    @property
    def is_paused(self) -> bool:
        """Check if the workflow is waiting for external input."""
        return self.state == WorkflowState.AWAITING_APPROVAL
