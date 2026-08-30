"""
Agent Registry — Cross-department agent discovery and lifecycle.

Provides a deterministic registry of all agents in the RecoveryOS fleet,
populated from the real capabilities already present in the codebase.
Each agent has an Agent Card describing its identity, capabilities,
allowed tools, data scopes, and operational status.

This is a RecoveryOS-native capability, not a GEAP Agent Registry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    """Operational status of a registered agent."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DEPRECATED = "DEPRECATED"


class AgentCard(BaseModel):
    """
    Structured identity card for a registered fleet agent.

    Contains the agent's capabilities, tool permissions, data scope
    boundaries, and operational metadata. This is the authoritative
    record used by the Agent Gateway for identity and scope validation.
    """

    agent_id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    owner: str = "recoveryos-platform"
    department: str = "operations"
    capabilities: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_data_scopes: list[str] = Field(default_factory=list)
    tenant_scope: str = "*"
    status: AgentStatus = AgentStatus.ACTIVE
    endpoint: str = "local://recoveryos"
    runtime: str = "google-adk"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_card_dict(self) -> dict[str, Any]:
        """Return the public agent card representation."""
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Fleet Agent Definitions
#
# Each agent below reflects REAL capabilities present in the RecoveryOS
# codebase. No fake external agents are invented.
# ---------------------------------------------------------------------------

_FLEET_AGENTS: list[AgentCard] = [
    AgentCard(
        agent_id="orchestrator",
        name="RecoveryOS Orchestrator",
        version="1.0.0",
        description=(
            "Top-level orchestrator agent that coordinates the customer "
            "onboarding workflow. Delegates execution to specialist agents "
            "and manages the conversation flow between them."
        ),
        owner="recoveryos-platform",
        department="operations",
        capabilities=["orchestration", "delegation", "workflow_coordination"],
        allowed_tools=[],  # Orchestrator delegates, does not call tools directly
        allowed_data_scopes=["customer.*", "workflow.*"],
        runtime="google-adk",
    ),
    AgentCard(
        agent_id="taskmaster",
        name="Taskmaster Agent",
        version="1.0.0",
        description=(
            "Primary execution agent with full mutating tool access. "
            "Executes onboarding steps, handles failures, and coordinates "
            "with the recovery specialist when issues arise."
        ),
        owner="recoveryos-platform",
        department="operations",
        capabilities=[
            "identity_verification",
            "document_validation",
            "risk_assessment",
            "billing",
            "account_activation",
            "notification",
            "verification",
            "diagnostics",
        ],
        allowed_tools=[
            "verify_identity",
            "validate_documents",
            "run_risk_check",
            "setup_billing",
            "activate_account",
            "send_welcome_package",
            "check_service_status",
            "list_available_billing_providers",
            "get_workflow_state",
            "verify_outcome",
        ],
        allowed_data_scopes=[
            "customer.identity",
            "customer.documents",
            "customer.risk",
            "customer.billing",
            "customer.account",
            "customer.notifications",
        ],
        runtime="google-adk",
    ),
    AgentCard(
        agent_id="recovery-specialist",
        name="Recovery Specialist",
        version="1.0.0",
        description=(
            "Diagnostic-only agent with READ-ONLY tools. Diagnoses failures "
            "and proposes structured recovery plans. Cannot mutate external "
            "state — separation of concerns between diagnosis and execution."
        ),
        owner="recoveryos-platform",
        department="recovery",
        capabilities=["failure_diagnosis", "recovery_planning", "service_status"],
        allowed_tools=[
            "check_service_status",
            "list_available_billing_providers",
            "get_workflow_state",
            "submit_recovery_plan",
        ],
        allowed_data_scopes=["customer.*", "workflow.*"],
        runtime="google-adk",
    ),
    AgentCard(
        agent_id="billing-agent",
        name="Billing Agent",
        version="1.0.0",
        description=(
            "Specialist agent for billing and subscription management. "
            "Handles provider failover (e.g. Stripe → PayPal) and billing "
            "plan configuration within policy boundaries."
        ),
        owner="recoveryos-platform",
        department="finance",
        capabilities=["billing", "subscription", "provider_failover"],
        allowed_tools=[
            "setup_billing",
            "list_available_billing_providers",
            "check_service_status",
            "verify_outcome",
        ],
        allowed_data_scopes=["customer.billing"],
        runtime="google-adk",
    ),
    AgentCard(
        agent_id="risk-agent",
        name="Risk Assessment Agent",
        version="1.0.0",
        description=(
            "Specialist agent for customer risk assessment and compliance "
            "scoring. Evaluates credit bureau data and enforces risk "
            "threshold constraints."
        ),
        owner="recoveryos-platform",
        department="compliance",
        capabilities=["risk_assessment", "compliance_scoring"],
        allowed_tools=[
            "run_risk_check",
            "check_service_status",
            "verify_outcome",
        ],
        allowed_data_scopes=["customer.risk", "customer.identity"],
        runtime="google-adk",
    ),
    AgentCard(
        agent_id="verification-agent",
        name="Verification Agent",
        version="1.0.0",
        description=(
            "Independent verification specialist. Performs separate queries "
            "to external services to confirm business outcomes match the "
            "OutcomeContract. A tool result alone cannot satisfy verification."
        ),
        owner="recoveryos-platform",
        department="compliance",
        capabilities=["independent_verification", "outcome_validation"],
        allowed_tools=[
            "verify_outcome",
            "get_workflow_state",
            "check_service_status",
        ],
        allowed_data_scopes=["customer.*", "workflow.*"],
        runtime="google-adk",
    ),
    AgentCard(
        agent_id="identity-agent",
        name="Identity Agent",
        version="1.0.0",
        description=(
            "Specialist agent for customer identity verification against "
            "government records. Must complete before any other onboarding "
            "step per the identity_first policy constraint."
        ),
        owner="recoveryos-platform",
        department="security",
        capabilities=["identity_verification", "kyc"],
        allowed_tools=[
            "verify_identity",
            "check_service_status",
            "verify_outcome",
        ],
        allowed_data_scopes=["customer.identity"],
        runtime="google-adk",
    ),
]


class AgentRegistry:
    """
    Deterministic in-memory agent registry.

    Populated at instantiation from the fleet agent definitions that
    reflect real capabilities in the RecoveryOS codebase. Provides
    read-only discovery and lookup operations.
    """

    def __init__(self, agents: list[AgentCard] | None = None):
        self._agents: dict[str, AgentCard] = {}
        for agent in (agents or _FLEET_AGENTS):
            self._agents[agent.agent_id] = agent

    def list_agents(
        self,
        department: str | None = None,
        status: AgentStatus | None = None,
        capability: str | None = None,
    ) -> list[AgentCard]:
        """List agents with optional filtering."""
        result = list(self._agents.values())

        if department:
            result = [a for a in result if a.department == department]
        if status:
            result = [a for a in result if a.status == status]
        if capability:
            result = [a for a in result if capability in a.capabilities]

        return result

    def get_agent(self, agent_id: str) -> AgentCard | None:
        """Get a single agent by ID."""
        return self._agents.get(agent_id)

    def get_agent_ids(self) -> list[str]:
        """Get all registered agent IDs."""
        return list(self._agents.keys())

    def get_agents_for_tool(self, tool_name: str) -> list[AgentCard]:
        """Find all agents authorized to use a specific tool."""
        return [
            a for a in self._agents.values()
            if tool_name in a.allowed_tools
        ]

    def get_agents_by_department(self) -> dict[str, list[AgentCard]]:
        """Group agents by department."""
        departments: dict[str, list[AgentCard]] = {}
        for agent in self._agents.values():
            departments.setdefault(agent.department, []).append(agent)
        return departments

    @property
    def agent_count(self) -> int:
        return len(self._agents)


# Module-level singleton
fleet_registry = AgentRegistry()
