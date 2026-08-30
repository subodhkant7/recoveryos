"""
Agent Identity — Zero-trust agent identity and scoped access.

Every registered agent gets a bound identity with tenant scope,
tool permissions, and data scope boundaries. Before an agent
invokes a protected tool, the identity layer validates:

    identity → capability check → tenant check → scope check

This is a RecoveryOS-native capability, not a GEAP Agent Identity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentRole(str, Enum):
    """Role classification for agent identities."""

    ORCHESTRATOR = "orchestrator"
    EXECUTOR = "executor"
    SPECIALIST = "specialist"
    VERIFIER = "verifier"
    DIAGNOSTICIAN = "diagnostician"


class AgentIdentity(BaseModel):
    """
    Bound identity for a fleet agent.

    Links an agent_id to its operational boundaries:
    tenant scope, role, allowed tools, and data scopes.
    """

    agent_id: str
    tenant_id: str = "*"
    role: AgentRole = AgentRole.SPECIALIST
    capabilities: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_data_scopes: list[str] = Field(default_factory=list)


class IdentityCheckResult(BaseModel):
    """Result of an agent identity validation check."""

    agent_id: str
    check: str
    passed: bool
    reason: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def validate_agent_tool_access(
    identity: AgentIdentity,
    tool_name: str,
    target_tenant_id: str = "tenant-default",
    data_scope: str | None = None,
) -> list[IdentityCheckResult]:
    """
    Validate whether an agent identity is authorized for a tool invocation.

    Runs three checks:
    1. Tool permission — is tool_name in agent's allowed_tools?
    2. Tenant scope — does agent's tenant_id match or is it wildcard?
    3. Data scope — is the requested data scope within allowed_data_scopes?

    Returns a list of check results. All must pass for access to be granted.
    """
    results: list[IdentityCheckResult] = []

    # 1. Tool permission check
    if identity.allowed_tools and tool_name not in identity.allowed_tools:
        results.append(IdentityCheckResult(
            agent_id=identity.agent_id,
            check="tool_permission",
            passed=False,
            reason=f"Agent '{identity.agent_id}' is not authorized for tool '{tool_name}'. "
                   f"Allowed tools: {identity.allowed_tools}",
        ))
    else:
        results.append(IdentityCheckResult(
            agent_id=identity.agent_id,
            check="tool_permission",
            passed=True,
            reason=f"Tool '{tool_name}' is in agent's allowed tool set",
        ))

    # 2. Tenant scope check
    if identity.tenant_id != "*" and identity.tenant_id != target_tenant_id:
        results.append(IdentityCheckResult(
            agent_id=identity.agent_id,
            check="tenant_scope",
            passed=False,
            reason=f"Agent '{identity.agent_id}' (tenant '{identity.tenant_id}') "
                   f"cannot access tenant '{target_tenant_id}'",
        ))
    else:
        results.append(IdentityCheckResult(
            agent_id=identity.agent_id,
            check="tenant_scope",
            passed=True,
            reason=f"Tenant scope valid (agent: '{identity.tenant_id}', target: '{target_tenant_id}')",
        ))

    # 3. Data scope check
    if data_scope and identity.allowed_data_scopes:
        scope_match = False
        for allowed in identity.allowed_data_scopes:
            if allowed == data_scope:
                scope_match = True
                break
            # Wildcard match: "customer.*" matches "customer.billing"
            if allowed.endswith(".*"):
                prefix = allowed[:-2]
                if data_scope.startswith(prefix):
                    scope_match = True
                    break

        if not scope_match:
            results.append(IdentityCheckResult(
                agent_id=identity.agent_id,
                check="data_scope",
                passed=False,
                reason=f"Agent '{identity.agent_id}' cannot access data scope '{data_scope}'. "
                       f"Allowed scopes: {identity.allowed_data_scopes}",
            ))
        else:
            results.append(IdentityCheckResult(
                agent_id=identity.agent_id,
                check="data_scope",
                passed=True,
                reason=f"Data scope '{data_scope}' is within agent's allowed scopes",
            ))
    elif data_scope and not identity.allowed_data_scopes:
        # No scope restrictions defined — allow
        results.append(IdentityCheckResult(
            agent_id=identity.agent_id,
            check="data_scope",
            passed=True,
            reason="No data scope restrictions defined for agent",
        ))

    return results


def identity_from_agent_card(card: Any) -> AgentIdentity:
    """
    Derive an AgentIdentity from an AgentCard.

    Maps agent_id, tenant_scope, capabilities, allowed_tools,
    and allowed_data_scopes from the card.
    """
    # Determine role from capabilities
    capabilities = getattr(card, "capabilities", []) or []
    if "orchestration" in capabilities:
        role = AgentRole.ORCHESTRATOR
    elif "independent_verification" in capabilities:
        role = AgentRole.VERIFIER
    elif "failure_diagnosis" in capabilities:
        role = AgentRole.DIAGNOSTICIAN
    else:
        role = AgentRole.SPECIALIST

    return AgentIdentity(
        agent_id=getattr(card, "agent_id", "unknown"),
        tenant_id=getattr(card, "tenant_scope", "*"),
        role=role,
        capabilities=capabilities,
        allowed_tools=getattr(card, "allowed_tools", []) or [],
        allowed_data_scopes=getattr(card, "allowed_data_scopes", []) or [],
    )
