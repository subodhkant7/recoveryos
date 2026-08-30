"""
Tests for Agent Registry — fleet agent discovery and lifecycle.
"""

import pytest

from backend.fleet.registry import (
    AgentCard,
    AgentRegistry,
    AgentStatus,
    fleet_registry,
)


class TestAgentRegistry:
    """Test agent registry discovery and lookup."""

    def test_fleet_registry_has_agents(self):
        """Registry is populated with fleet agents."""
        assert fleet_registry.agent_count >= 7

    def test_list_all_agents(self):
        """List all returns all registered agents."""
        agents = fleet_registry.list_agents()
        assert len(agents) >= 7
        ids = [a.agent_id for a in agents]
        assert "orchestrator" in ids
        assert "taskmaster" in ids
        assert "recovery-specialist" in ids
        assert "billing-agent" in ids
        assert "risk-agent" in ids
        assert "verification-agent" in ids
        assert "identity-agent" in ids

    def test_get_agent_by_id(self):
        """Get returns the correct agent card."""
        agent = fleet_registry.get_agent("billing-agent")
        assert agent is not None
        assert agent.agent_id == "billing-agent"
        assert agent.department == "finance"
        assert "billing" in agent.capabilities
        assert "setup_billing" in agent.allowed_tools

    def test_get_unknown_agent_returns_none(self):
        """Unknown agent returns None."""
        assert fleet_registry.get_agent("nonexistent-agent") is None

    def test_filter_by_department(self):
        """Filter by department returns only matching agents."""
        compliance = fleet_registry.list_agents(department="compliance")
        assert all(a.department == "compliance" for a in compliance)
        assert len(compliance) >= 2  # risk-agent and verification-agent

    def test_filter_by_capability(self):
        """Filter by capability returns agents with that capability."""
        billing = fleet_registry.list_agents(capability="billing")
        assert all("billing" in a.capabilities for a in billing)

    def test_filter_by_status(self):
        """Filter by status returns only matching agents."""
        active = fleet_registry.list_agents(status=AgentStatus.ACTIVE)
        assert all(a.status == AgentStatus.ACTIVE for a in active)

    def test_versions_are_deterministic(self):
        """Agent versions are deterministic (not random)."""
        agent = fleet_registry.get_agent("billing-agent")
        assert agent.version == "1.0.0"
        # Get again — same version
        agent2 = fleet_registry.get_agent("billing-agent")
        assert agent2.version == agent.version

    def test_agent_card_serialization(self):
        """Agent card serializes to dict correctly."""
        agent = fleet_registry.get_agent("orchestrator")
        card = agent.to_card_dict()
        assert isinstance(card, dict)
        assert card["agent_id"] == "orchestrator"
        assert card["status"] == "ACTIVE"
        assert "capabilities" in card

    def test_get_agents_for_tool(self):
        """Find agents authorized for a specific tool."""
        agents = fleet_registry.get_agents_for_tool("setup_billing")
        agent_ids = [a.agent_id for a in agents]
        assert "billing-agent" in agent_ids
        assert "taskmaster" in agent_ids

    def test_get_agents_by_department(self):
        """Group agents by department."""
        by_dept = fleet_registry.get_agents_by_department()
        assert "operations" in by_dept
        assert "finance" in by_dept
        assert "compliance" in by_dept

    def test_orchestrator_has_no_direct_tools(self):
        """Orchestrator delegates — it does not call tools directly."""
        agent = fleet_registry.get_agent("orchestrator")
        assert agent.allowed_tools == []

    def test_recovery_specialist_is_read_only(self):
        """Recovery specialist has only diagnostic/read-only tools."""
        agent = fleet_registry.get_agent("recovery-specialist")
        mutating = {"verify_identity", "validate_documents", "run_risk_check",
                     "setup_billing", "activate_account", "send_welcome_package"}
        for tool in agent.allowed_tools:
            assert tool not in mutating, f"Recovery specialist should not have mutating tool '{tool}'"

    def test_custom_registry(self):
        """Custom registry can be created with custom agents."""
        custom_agent = AgentCard(
            agent_id="test-agent",
            name="Test Agent",
            capabilities=["testing"],
            allowed_tools=["check_service_status"],
        )
        registry = AgentRegistry(agents=[custom_agent])
        assert registry.agent_count == 1
        assert registry.get_agent("test-agent") is not None
