"""
RecoveryOS domain models.

All models use Pydantic for serialization to/from Firestore.
"""

from backend.models.workflow import (
    Constraint,
    OutcomeContract,
    RequiredOutcome,
    StepStatus,
    Workflow,
    WorkflowState,
    WorkflowStep,
    VALID_TRANSITIONS,
)
from backend.models.evidence import (
    Evidence,
    EvidenceType,
    VerificationResult,
)
from backend.models.recovery import (
    Confidence,
    Failure,
    FailureType,
    RecoveryPlan,
    RecoveryPlanStatus,
    RecoveryStep,
)
from backend.models.policy import (
    PolicyDecision,
    PolicyOutcome,
    PolicyRuleResult,
)
from backend.models.approval import (
    ApprovalStatus,
    HumanApproval,
)
from backend.models.idempotency import (
    IdempotencyRecord,
    IdempotencyStatus,
)
from backend.models.events import (
    EventType,
    WorkflowEvent,
)

__all__ = [
    # Workflow
    "Constraint",
    "OutcomeContract",
    "RequiredOutcome",
    "StepStatus",
    "VALID_TRANSITIONS",
    "Workflow",
    "WorkflowState",
    "WorkflowStep",
    # Evidence
    "Evidence",
    "EvidenceType",
    "VerificationResult",
    # Recovery
    "Confidence",
    "Failure",
    "FailureType",
    "RecoveryPlan",
    "RecoveryPlanStatus",
    "RecoveryStep",
    # Policy
    "PolicyDecision",
    "PolicyOutcome",
    "PolicyRuleResult",
    # Approval
    "ApprovalStatus",
    "HumanApproval",
    # Idempotency
    "IdempotencyRecord",
    # Events
    "EventType",
    "WorkflowEvent",
]
