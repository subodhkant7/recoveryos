"""
Tests for Package Installation and Configuration.

Verifies:
F. The recoveryos package installs, imports cleanly, and provides valid configuration defaults.
"""

from backend.config import config
from backend.models.workflow import Workflow, WorkflowState, OutcomeContract, RequiredOutcome
from backend.models.events import EventType, WorkflowEvent
from backend.models.evidence import Evidence, EvidenceType, VerificationResult


def test_package_and_models_import():
    """Verify models instantiate and validate as expected."""
    outcome = RequiredOutcome(
        outcome_id="test_outcome",
        description="Test description",
        acceptance_criteria={"key": "val"},
        verified=False,
    )
    contract = OutcomeContract(
        workflow_id="wf-123",
        required_outcomes=[outcome],
    )
    assert not contract.all_verified()

    outcome.verified = True
    assert contract.all_verified()


def test_config_defaults():
    """Verify configuration defaults are appropriate for hackathon compliance."""
    assert config.gemini_model in ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.7-flash")
    assert config.port == 8000
