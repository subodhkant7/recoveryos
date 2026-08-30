"""
Tests for Agent Identity — zero-trust agent identity and scoped access.
"""

import pytest

from backend.fleet.identity import (
    AgentIdentity,
    AgentRole,
    IdentityCheckResult,
    identity_from_agent_card,
    validate_agent_tool_access,
)
from backend.fleet.registry import fleet_registry


class TestAgentIdentity:
    """Test agent identity validation."""

    def test_valid_tool_access(self):
        """Agent with tool in allowed_tools passes."""
        identity = AgentIdentity(
            agent_id="billing-agent",
            allowed_tools=["setup_billing", "verify_outcome"],
            allowed_data_scopes=["customer.billing"],
        )
        results = validate_agent_tool_access(
            identity, "setup_billing", "tenant-default", "customer.billing"
        )
        assert all(r.passed for r in results)

    def test_unauthorized_tool_denied(self):
        """Agent without tool in allowed_tools is denied."""
        identity = AgentIdentity(
            agent_id="billing-agent",
            allowed_tools=["setup_billing"],
            allowed_data_scopes=["customer.billing"],
        )
        results = validate_agent_tool_access(identity, "run_risk_check")
        tool_check = [r for r in results if r.check == "tool_permission"]
        assert len(tool_check) == 1
        assert not tool_check[0].passed

    def test_wrong_tenant_denied(self):
        """Agent scoped to specific tenant cannot access other tenant."""
        identity = AgentIdentity(
            agent_id="billing-agent",
            tenant_id="tenant-acme",
            allowed_tools=["setup_billing"],
        )
        results = validate_agent_tool_access(
            identity, "setup_billing", "tenant-other"
        )
        tenant_check = [r for r in results if r.check == "tenant_scope"]
        assert len(tenant_check) == 1
        assert not tenant_check[0].passed

    def test_wildcard_tenant_allows_all(self):
        """Agent with wildcard tenant can access any tenant."""
        identity = AgentIdentity(
            agent_id="orchestrator",
            tenant_id="*",
            allowed_tools=[],
        )
        results = validate_agent_tool_access(
            identity, "verify_identity", "tenant-anything"
        )
        tenant_check = [r for r in results if r.check == "tenant_scope"]
        assert all(r.passed for r in tenant_check)

    def test_out_of_scope_data_denied(self):
        """Agent requesting data outside its scopes is denied."""
        identity = AgentIdentity(
            agent_id="billing-agent",
            allowed_tools=["setup_billing"],
            allowed_data_scopes=["customer.billing"],
        )
        results = validate_agent_tool_access(
            identity, "setup_billing", "tenant-default", "customer.identity"
        )
        scope_check = [r for r in results if r.check == "data_scope"]
        assert len(scope_check) == 1
        assert not scope_check[0].passed

    def test_wildcard_scope_matches(self):
        """Wildcard data scope matches sub-scopes."""
        identity = AgentIdentity(
            agent_id="orchestrator",
            allowed_tools=[],
            allowed_data_scopes=["customer.*"],
        )
        results = validate_agent_tool_access(
            identity, "verify_identity", "tenant-default", "customer.billing"
        )
        scope_check = [r for r in results if r.check == "data_scope"]
        assert all(r.passed for r in scope_check)

    def test_identity_from_agent_card(self):
        """Identity derived from agent card has correct attributes."""
        card = fleet_registry.get_agent("billing-agent")
        identity = identity_from_agent_card(card)
        assert identity.agent_id == "billing-agent"
        assert identity.role == AgentRole.SPECIALIST
        assert "setup_billing" in identity.allowed_tools
        assert "customer.billing" in identity.allowed_data_scopes

    def test_orchestrator_role_detection(self):
        """Orchestrator agent gets ORCHESTRATOR role."""
        card = fleet_registry.get_agent("orchestrator")
        identity = identity_from_agent_card(card)
        assert identity.role == AgentRole.ORCHESTRATOR

    def test_verifier_role_detection(self):
        """Verification agent gets VERIFIER role."""
        card = fleet_registry.get_agent("verification-agent")
        identity = identity_from_agent_card(card)
        assert identity.role == AgentRole.VERIFIER

    def test_diagnostician_role_detection(self):
        """Recovery specialist gets DIAGNOSTICIAN role."""
        card = fleet_registry.get_agent("recovery-specialist")
        identity = identity_from_agent_card(card)
        assert identity.role == AgentRole.DIAGNOSTICIAN

    def test_no_data_scope_restriction_allows(self):
        """Agent with no data scope restrictions allows any data scope."""
        identity = AgentIdentity(
            agent_id="test-agent",
            allowed_tools=["verify_identity"],
            allowed_data_scopes=[],
        )
        results = validate_agent_tool_access(
            identity, "verify_identity", "tenant-default", "customer.identity"
        )
        scope_check = [r for r in results if r.check == "data_scope"]
        assert all(r.passed for r in scope_check)
