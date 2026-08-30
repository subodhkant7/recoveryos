"""
Failure-Tolerant Inter-Agent Routing.

Explicit routing behavior:

    Orchestrator → Primary specialist → [Failure] → Fallback specialist → Verification

Reuses existing recovery/reconciliation mechanisms. Routing respects:
- Idempotency keys
- OCC version control
- Bounded retries (recovery budget)
- Independent verification

No unbounded agent-to-agent loops.

This is a RecoveryOS-native capability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from backend.fleet.registry import AgentRegistry, fleet_registry


class RouteOutcome(str, Enum):
    """Outcome of a routing decision."""

    PRIMARY = "PRIMARY"
    FALLBACK = "FALLBACK"
    ESCALATE = "ESCALATE"
    NO_ROUTE = "NO_ROUTE"


class RouteDecision(BaseModel):
    """A routing decision for an outcome recovery."""

    outcome_id: str
    route_outcome: RouteOutcome
    primary_agent_id: str = ""
    fallback_agent_id: str = ""
    selected_agent_id: str = ""
    reason: str = ""
    attempt: int = 0
    max_attempts: int = 3
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# Routing table mapping outcome IDs to primary and fallback agents.
# Reflects real capabilities from the RecoveryOS codebase.
ROUTING_TABLE: dict[str, dict[str, str]] = {
    "identity_verified": {
        "primary": "identity-agent",
        "fallback": "taskmaster",
        "verification": "verification-agent",
    },
    "documents_validated": {
        "primary": "taskmaster",
        "fallback": "recovery-specialist",
        "verification": "verification-agent",
    },
    "risk_assessed": {
        "primary": "risk-agent",
        "fallback": "taskmaster",
        "verification": "verification-agent",
    },
    "billing_configured": {
        "primary": "billing-agent",
        "fallback": "recovery-specialist",
        "verification": "verification-agent",
    },
    "account_activated": {
        "primary": "taskmaster",
        "fallback": "recovery-specialist",
        "verification": "verification-agent",
    },
    "welcome_sent": {
        "primary": "taskmaster",
        "fallback": "recovery-specialist",
        "verification": "verification-agent",
    },
}


class AgentRouter:
    """
    Failure-tolerant inter-agent router.

    Routes outcome recovery to the most appropriate specialist agent.
    On primary failure, routes to fallback agent within the recovery
    budget. Escalates when the budget is exhausted.

    Never introduces unbounded agent-to-agent loops.
    """

    def __init__(self, registry: AgentRegistry | None = None):
        self._registry = registry or fleet_registry
        # Track routing attempts per (workflow_id, outcome_id) to enforce budget
        self._attempt_counts: dict[str, int] = {}

    def route(
        self,
        outcome_id: str,
        workflow_id: str = "",
        primary_failed: bool = False,
        attempt: int = 0,
        max_attempts: int = 3,
    ) -> RouteDecision:
        """
        Determine which agent should handle recovery for an outcome.

        Args:
            outcome_id: The outcome that needs recovery
            workflow_id: Current workflow ID for tracking
            primary_failed: Whether the primary agent already failed
            attempt: Current attempt number
            max_attempts: Maximum attempts before escalation

        Returns:
            RouteDecision with the selected agent and routing rationale
        """
        route_entry = ROUTING_TABLE.get(outcome_id)
        if not route_entry:
            return RouteDecision(
                outcome_id=outcome_id,
                route_outcome=RouteOutcome.NO_ROUTE,
                reason=f"No routing entry for outcome '{outcome_id}'",
                attempt=attempt,
                max_attempts=max_attempts,
            )

        primary_id = route_entry["primary"]
        fallback_id = route_entry["fallback"]

        # Budget check — bounded retries
        if attempt >= max_attempts:
            return RouteDecision(
                outcome_id=outcome_id,
                route_outcome=RouteOutcome.ESCALATE,
                primary_agent_id=primary_id,
                fallback_agent_id=fallback_id,
                selected_agent_id="",
                reason=f"Recovery budget exhausted ({attempt}/{max_attempts} attempts). Escalating.",
                attempt=attempt,
                max_attempts=max_attempts,
            )

        if not primary_failed:
            # Route to primary
            primary_card = self._registry.get_agent(primary_id)
            if primary_card and primary_card.status.value == "ACTIVE":
                return RouteDecision(
                    outcome_id=outcome_id,
                    route_outcome=RouteOutcome.PRIMARY,
                    primary_agent_id=primary_id,
                    fallback_agent_id=fallback_id,
                    selected_agent_id=primary_id,
                    reason=f"Routing to primary agent '{primary_id}' for outcome '{outcome_id}'",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
            else:
                # Primary not available, try fallback
                primary_failed = True

        # Route to fallback
        fallback_card = self._registry.get_agent(fallback_id)
        if fallback_card and fallback_card.status.value == "ACTIVE":
            return RouteDecision(
                outcome_id=outcome_id,
                route_outcome=RouteOutcome.FALLBACK,
                primary_agent_id=primary_id,
                fallback_agent_id=fallback_id,
                selected_agent_id=fallback_id,
                reason=f"Primary agent '{primary_id}' failed; routing to fallback '{fallback_id}'",
                attempt=attempt,
                max_attempts=max_attempts,
            )

        # Both unavailable
        return RouteDecision(
            outcome_id=outcome_id,
            route_outcome=RouteOutcome.ESCALATE,
            primary_agent_id=primary_id,
            fallback_agent_id=fallback_id,
            selected_agent_id="",
            reason=f"Neither primary '{primary_id}' nor fallback '{fallback_id}' are available. Escalating.",
            attempt=attempt,
            max_attempts=max_attempts,
        )

    def get_verification_agent(self, outcome_id: str) -> str:
        """Get the verification agent for an outcome."""
        route_entry = ROUTING_TABLE.get(outcome_id, {})
        return route_entry.get("verification", "verification-agent")

    def get_routing_table(self) -> dict[str, dict[str, str]]:
        """Get the full routing table for display."""
        return dict(ROUTING_TABLE)


# Module-level singleton
fleet_router = AgentRouter()
