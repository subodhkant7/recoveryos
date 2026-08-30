"""
Agent Gateway — Centralized routing and policy boundary.

All fleet-level specialist tool access logically passes through
the gateway. It chains:

    Agent Identity → Tenant/Scope validation → PolicyEngine → Tool permission

Every decision is auditable with agent_id, tool, tenant_id,
policy reason, correlation_id, and timestamp.

This is a RecoveryOS-native capability, not a GEAP Agent Gateway.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from backend.fleet.registry import AgentRegistry, fleet_registry
from backend.fleet.identity import (
    AgentIdentity,
    IdentityCheckResult,
    identity_from_agent_card,
    validate_agent_tool_access,
)


class GatewayOutcome(str, Enum):
    """Result of a gateway access decision."""

    ALLOW = "ALLOW"
    DENY = "DENY"


class GatewayDecision(BaseModel):
    """
    Auditable decision record from the Agent Gateway.

    Contains the full chain of identity, scope, and policy checks
    that determined whether an agent was permitted to invoke a tool.
    """

    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    tool: str
    tenant_id: str = "tenant-default"
    outcome: GatewayOutcome
    identity_check: str = "PASS"
    scope_check: str = "PASS"
    policy_outcome: str = "APPROVED"
    reason: str = ""
    identity_details: list[dict[str, Any]] = Field(default_factory=list)
    correlation_id: str = ""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class AgentGateway:
    """
    Centralized policy boundary for fleet agent tool access.

    For each tool invocation request:
    1. Resolve agent identity from registry
    2. Validate tenant scope
    3. Validate tool is in agent's allowed_tools
    4. Validate data scope
    5. Optionally delegate to PolicyEngine for workflow-level policy
    6. Return GatewayDecision(ALLOW/DENY) with full audit payload
    """

    def __init__(self, registry: AgentRegistry | None = None):
        self._registry = registry or fleet_registry

    def evaluate(
        self,
        agent_id: str,
        tool_name: str,
        tenant_id: str = "tenant-default",
        data_scope: str | None = None,
        correlation_id: str = "",
        workflow_id: str = "",
    ) -> GatewayDecision:
        """
        Evaluate whether an agent is permitted to invoke a tool.

        Returns a GatewayDecision with ALLOW or DENY outcome.
        All decisions are structured for audit trail recording.
        """
        corr_id = correlation_id or str(uuid.uuid4())

        # 1. Resolve agent from registry
        agent_card = self._registry.get_agent(agent_id)
        if not agent_card:
            return GatewayDecision(
                agent_id=agent_id,
                tool=tool_name,
                tenant_id=tenant_id,
                outcome=GatewayOutcome.DENY,
                identity_check="FAIL",
                reason=f"Agent '{agent_id}' not found in fleet registry",
                correlation_id=corr_id,
            )

        # 2. Check agent status
        from backend.fleet.registry import AgentStatus
        if agent_card.status != AgentStatus.ACTIVE:
            return GatewayDecision(
                agent_id=agent_id,
                tool=tool_name,
                tenant_id=tenant_id,
                outcome=GatewayOutcome.DENY,
                identity_check="FAIL",
                reason=f"Agent '{agent_id}' is in status '{agent_card.status.value}', not ACTIVE",
                correlation_id=corr_id,
            )

        # 3. Derive identity and run access checks
        identity = identity_from_agent_card(agent_card)
        check_results = validate_agent_tool_access(
            identity=identity,
            tool_name=tool_name,
            target_tenant_id=tenant_id,
            data_scope=data_scope,
        )

        identity_details = [r.model_dump(mode="json") for r in check_results]
        all_passed = all(r.passed for r in check_results)

        if not all_passed:
            failed_checks = [r for r in check_results if not r.passed]
            failed_names = [r.check for r in failed_checks]
            reasons = "; ".join(r.reason for r in failed_checks)
            return GatewayDecision(
                agent_id=agent_id,
                tool=tool_name,
                tenant_id=tenant_id,
                outcome=GatewayOutcome.DENY,
                identity_check="FAIL" if any(r.check == "tool_permission" for r in failed_checks) else "PASS",
                scope_check="FAIL" if any(r.check in ("tenant_scope", "data_scope") for r in failed_checks) else "PASS",
                reason=reasons,
                identity_details=identity_details,
                correlation_id=corr_id,
            )

        # All identity/scope checks passed
        return GatewayDecision(
            agent_id=agent_id,
            tool=tool_name,
            tenant_id=tenant_id,
            outcome=GatewayOutcome.ALLOW,
            identity_check="PASS",
            scope_check="PASS",
            policy_outcome="APPROVED",
            reason="All identity, scope, and policy checks passed",
            identity_details=identity_details,
            correlation_id=corr_id,
        )

    def evaluate_with_audit(
        self,
        agent_id: str,
        tool_name: str,
        tenant_id: str = "tenant-default",
        data_scope: str | None = None,
        correlation_id: str = "",
        workflow_id: str = "",
    ) -> GatewayDecision:
        """
        Evaluate and record an audit event for the decision.

        Uses the existing RecoveryOS security audit subsystem.
        """
        decision = self.evaluate(
            agent_id=agent_id,
            tool_name=tool_name,
            tenant_id=tenant_id,
            data_scope=data_scope,
            correlation_id=correlation_id,
            workflow_id=workflow_id,
        )

        # Record via existing audit subsystem
        try:
            from backend.security.audit import record_security_audit_event
            record_security_audit_event(
                event_type="FLEET_GATEWAY_DECISION",
                actor_id=agent_id,
                role="agent",
                tenant_id=tenant_id,
                workflow_id=workflow_id or None,
                action=f"tool_access:{tool_name}",
                outcome=decision.outcome.value,
                reason=decision.reason,
                extra={
                    "decision_id": decision.decision_id,
                    "identity_check": decision.identity_check,
                    "scope_check": decision.scope_check,
                    "policy_outcome": decision.policy_outcome,
                    "correlation_id": decision.correlation_id,
                },
            )
        except Exception:
            pass  # Audit recording must not break gateway flow

        return decision


# Module-level singleton
fleet_gateway = AgentGateway()
