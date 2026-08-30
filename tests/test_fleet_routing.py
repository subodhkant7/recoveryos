"""
Tests for Failure-Tolerant Inter-Agent Routing.
"""

import pytest

from backend.fleet.routing import (
    AgentRouter,
    RouteDecision,
    RouteOutcome,
    fleet_router,
    ROUTING_TABLE,
)
from backend.fleet.registry import AgentCard, AgentRegistry, AgentStatus


class TestFleetRouting:
    """Test failure-tolerant inter-agent routing behavior."""

    def test_routing_table_completeness(self):
        """Routing table covers key required workflow outcomes."""
        assert "identity_verified" in ROUTING_TABLE
        assert "documents_validated" in ROUTING_TABLE
        assert "risk_assessed" in ROUTING_TABLE
        assert "billing_configured" in ROUTING_TABLE
        assert "account_activated" in ROUTING_TABLE
        assert "welcome_sent" in ROUTING_TABLE

    def test_route_to_primary_agent(self):
        """Initial routing targets the primary specialist agent."""
        decision = fleet_router.route(
            outcome_id="billing_configured",
            primary_failed=False,
            attempt=0,
        )
        assert decision.route_outcome == RouteOutcome.PRIMARY
        assert decision.selected_agent_id == "billing-agent"
        assert decision.primary_agent_id == "billing-agent"
        assert decision.fallback_agent_id == "recovery-specialist"

    def test_specialist_failure_routes_to_fallback(self):
        """When primary specialist fails, routes to fallback agent."""
        decision = fleet_router.route(
            outcome_id="billing_configured",
            primary_failed=True,
            attempt=1,
        )
        assert decision.route_outcome == RouteOutcome.FALLBACK
        assert decision.selected_agent_id == "recovery-specialist"
        assert "recovery-specialist" in decision.reason

    def test_budget_exhaustion_escalates(self):
        """Exhausting the recovery budget triggers escalation (no infinite loops)."""
        decision = fleet_router.route(
            outcome_id="billing_configured",
            primary_failed=True,
            attempt=3,
            max_attempts=3,
        )
        assert decision.route_outcome == RouteOutcome.ESCALATE
        assert "budget exhausted" in decision.reason.lower()
        assert decision.selected_agent_id == ""

    def test_unknown_outcome_no_route(self):
        """Unknown outcome returns NO_ROUTE."""
        decision = fleet_router.route(
            outcome_id="unknown_outcome_id",
            attempt=0,
        )
        assert decision.route_outcome == RouteOutcome.NO_ROUTE

    def test_verification_agent_mapping(self):
        """Verification agent is mapped for outcomes."""
        agent = fleet_router.get_verification_agent("billing_configured")
        assert agent == "verification-agent"

    def test_custom_registry_primary_unavailable_routes_to_fallback(self):
        """If primary agent is suspended/missing, automatically routes to fallback."""
        primary_agent = AgentCard(
            agent_id="primary-agent",
            name="Primary",
            status=AgentStatus.SUSPENDED,
            allowed_tools=["setup_billing"],
        )
        fallback_agent = AgentCard(
            agent_id="fallback-agent",
            name="Fallback",
            status=AgentStatus.ACTIVE,
            allowed_tools=["setup_billing"],
        )
        registry = AgentRegistry(agents=[primary_agent, fallback_agent])
        router = AgentRouter(registry=registry)

        # Mock ROUTING_TABLE lookup behavior with custom router
        router_table = {
            "custom_outcome": {
                "primary": "primary-agent",
                "fallback": "fallback-agent",
            }
        }
        import backend.fleet.routing as routing_mod
        original_table = routing_mod.ROUTING_TABLE
        try:
            routing_mod.ROUTING_TABLE = router_table
            decision = router.route("custom_outcome", primary_failed=False, attempt=0)
            assert decision.route_outcome == RouteOutcome.FALLBACK
            assert decision.selected_agent_id == "fallback-agent"
        finally:
            routing_mod.ROUTING_TABLE = original_table
