"""
Tests for Agent Gateway — centralized routing and policy boundary.
"""

import pytest

from backend.fleet.gateway import (
    AgentGateway,
    GatewayDecision,
    GatewayOutcome,
    fleet_gateway,
)
from backend.fleet.registry import AgentCard, AgentRegistry, AgentStatus


class TestAgentGateway:
    """Test agent gateway access decisions."""

    def test_valid_access_allowed(self):
        """Valid agent/tool/tenant/scope combination is allowed."""
        decision = fleet_gateway.evaluate(
            agent_id="billing-agent",
            tool_name="setup_billing",
            tenant_id="tenant-default",
            data_scope="customer.billing",
        )
        assert decision.outcome == GatewayOutcome.ALLOW
        assert decision.identity_check == "PASS"
        assert decision.scope_check == "PASS"

    def test_unknown_agent_denied(self):
        """Unknown agent is denied."""
        decision = fleet_gateway.evaluate(
            agent_id="nonexistent-agent",
            tool_name="setup_billing",
        )
        assert decision.outcome == GatewayOutcome.DENY
        assert "not found" in decision.reason.lower()

    def test_unauthorized_tool_denied(self):
        """Agent cannot use a tool not in its allowed set."""
        decision = fleet_gateway.evaluate(
            agent_id="billing-agent",
            tool_name="verify_identity",
            tenant_id="tenant-default",
        )
        assert decision.outcome == GatewayOutcome.DENY
        assert decision.identity_check == "FAIL"

    def test_wrong_tenant_denied(self):
        """Agent scoped to specific tenant cannot access other tenant."""
        # Create a custom registry with tenant-scoped agent
        scoped_agent = AgentCard(
            agent_id="scoped-agent",
            name="Scoped Agent",
            tenant_scope="tenant-acme",
            allowed_tools=["setup_billing"],
            capabilities=["billing"],
        )
        registry = AgentRegistry(agents=[scoped_agent])
        gateway = AgentGateway(registry=registry)

        decision = gateway.evaluate(
            agent_id="scoped-agent",
            tool_name="setup_billing",
            tenant_id="tenant-other",
        )
        assert decision.outcome == GatewayOutcome.DENY
        assert decision.scope_check == "FAIL"

    def test_out_of_scope_data_denied(self):
        """Agent requesting data outside its scopes is denied."""
        decision = fleet_gateway.evaluate(
            agent_id="billing-agent",
            tool_name="setup_billing",
            data_scope="customer.identity",
        )
        assert decision.outcome == GatewayOutcome.DENY

    def test_policy_decision_recorded(self):
        """Gateway decision contains full audit chain."""
        decision = fleet_gateway.evaluate(
            agent_id="billing-agent",
            tool_name="setup_billing",
            tenant_id="tenant-default",
            data_scope="customer.billing",
            correlation_id="test-corr-123",
        )
        assert decision.decision_id
        assert decision.correlation_id == "test-corr-123"
        assert decision.timestamp
        assert decision.agent_id == "billing-agent"
        assert decision.tool == "setup_billing"

    def test_identity_details_included(self):
        """Gateway decision includes identity check details."""
        decision = fleet_gateway.evaluate(
            agent_id="billing-agent",
            tool_name="setup_billing",
            data_scope="customer.billing",
        )
        assert len(decision.identity_details) > 0

    def test_suspended_agent_denied(self):
        """Suspended agent is denied."""
        suspended = AgentCard(
            agent_id="suspended-agent",
            name="Suspended",
            status=AgentStatus.SUSPENDED,
            allowed_tools=["setup_billing"],
            capabilities=["billing"],
        )
        registry = AgentRegistry(agents=[suspended])
        gateway = AgentGateway(registry=registry)

        decision = gateway.evaluate(
            agent_id="suspended-agent",
            tool_name="setup_billing",
        )
        assert decision.outcome == GatewayOutcome.DENY
        assert "SUSPENDED" in decision.reason

    def test_audit_integration(self):
        """evaluate_with_audit records security audit event."""
        from backend.security.audit import get_security_audit_logs, clear_security_audit_logs
        clear_security_audit_logs()

        fleet_gateway.evaluate_with_audit(
            agent_id="billing-agent",
            tool_name="setup_billing",
            tenant_id="tenant-default",
            data_scope="customer.billing",
            workflow_id="test-wf-audit",
        )

        logs = get_security_audit_logs()
        fleet_logs = [l for l in logs if l.get("event_type") == "FLEET_GATEWAY_DECISION"]
        assert len(fleet_logs) >= 1
        assert fleet_logs[-1]["workflow_id"] == "test-wf-audit"
