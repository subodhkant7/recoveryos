"""
Agent Guardrails — Deterministic safety inspection.

Lightweight first-party safety component that inspects tool requests
for sensitive fields, unauthorized scopes, unsafe combinations, and
obvious injection indicators.

Returns ALLOW / BLOCK / REVIEW.

This is RecoveryOS Agent Guardrails, not GEAP Model Armor.
Conservative deterministic checks only — must not break legitimate
demo workflows.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class GuardrailOutcome(str, Enum):
    """Result of a guardrail inspection."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REVIEW = "REVIEW"


class GuardrailResult(BaseModel):
    """Result of a guardrail safety inspection."""

    outcome: GuardrailOutcome
    check_name: str
    reason: str
    agent_id: str = ""
    tool_name: str = ""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# Patterns for sensitive field detection
_SENSITIVE_PATTERNS = re.compile(
    r"(?i)(password|secret|private_key|api_key|access_token|"
    r"refresh_token|ssn|social_security|credit_card|cvv|"
    r"bank_account|routing_number)"
)

# Patterns for obvious prompt/tool injection indicators
_INJECTION_PATTERNS = re.compile(
    r"(?i)(ignore\s+(previous|all|above)\s+instructions|"
    r"override\s+policy|bypass\s+security|"
    r"execute\s+system\s+command|"
    r"<script|javascript:|eval\(|exec\(|"
    r"rm\s+-rf|drop\s+table)"
)

# Known tool set from the RecoveryOS onboarding workflow
_KNOWN_TOOLS = {
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
    "submit_recovery_plan",
}

# Tool data scope mapping
_TOOL_DATA_SCOPES: dict[str, str] = {
    "verify_identity": "customer.identity",
    "validate_documents": "customer.documents",
    "run_risk_check": "customer.risk",
    "setup_billing": "customer.billing",
    "activate_account": "customer.account",
    "send_welcome_package": "customer.notifications",
    "verify_outcome": "customer.*",
    "check_service_status": "system.status",
    "list_available_billing_providers": "system.providers",
    "get_workflow_state": "workflow.*",
    "submit_recovery_plan": "workflow.*",
}


class AgentGuardrails:
    """
    Deterministic safety inspector for agent tool requests.

    Runs conservative checks that will never block legitimate
    RecoveryOS demo workflows but will catch obvious safety violations.
    """

    def inspect(
        self,
        agent_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        agent_allowed_tools: list[str] | None = None,
        agent_allowed_scopes: list[str] | None = None,
    ) -> list[GuardrailResult]:
        """
        Run all guardrail checks on a tool invocation request.

        Returns a list of check results. If any result has outcome
        BLOCK, the invocation should be rejected.
        """
        results: list[GuardrailResult] = []

        results.append(self._check_unknown_tool(agent_id, tool_name))
        results.append(self._check_sensitive_fields(agent_id, tool_name, tool_args))
        results.append(self._check_injection(agent_id, tool_name, tool_args))
        results.append(self._check_scope_authorization(
            agent_id, tool_name, agent_allowed_scopes
        ))

        if agent_allowed_tools is not None:
            results.append(self._check_tool_authorization(
                agent_id, tool_name, agent_allowed_tools
            ))

        return results

    def get_overall_outcome(self, results: list[GuardrailResult]) -> GuardrailOutcome:
        """Determine the most restrictive outcome from a list of check results."""
        if any(r.outcome == GuardrailOutcome.BLOCK for r in results):
            return GuardrailOutcome.BLOCK
        if any(r.outcome == GuardrailOutcome.REVIEW for r in results):
            return GuardrailOutcome.REVIEW
        return GuardrailOutcome.ALLOW

    def _check_unknown_tool(
        self, agent_id: str, tool_name: str,
    ) -> GuardrailResult:
        """Block invocations of tools not in the known tool set."""
        if tool_name not in _KNOWN_TOOLS:
            return GuardrailResult(
                outcome=GuardrailOutcome.BLOCK,
                check_name="unknown_tool",
                reason=f"Tool '{tool_name}' is not in the known RecoveryOS tool set",
                agent_id=agent_id,
                tool_name=tool_name,
            )
        return GuardrailResult(
            outcome=GuardrailOutcome.ALLOW,
            check_name="unknown_tool",
            reason=f"Tool '{tool_name}' is a known RecoveryOS tool",
            agent_id=agent_id,
            tool_name=tool_name,
        )

    def _check_sensitive_fields(
        self, agent_id: str, tool_name: str, tool_args: dict[str, Any],
    ) -> GuardrailResult:
        """Block tool args containing raw credentials or PII markers."""
        for key, value in tool_args.items():
            if _SENSITIVE_PATTERNS.search(key):
                return GuardrailResult(
                    outcome=GuardrailOutcome.BLOCK,
                    check_name="sensitive_field",
                    reason=f"Tool argument key '{key}' matches sensitive data pattern",
                    agent_id=agent_id,
                    tool_name=tool_name,
                )
            if isinstance(value, str) and _SENSITIVE_PATTERNS.search(value):
                return GuardrailResult(
                    outcome=GuardrailOutcome.REVIEW,
                    check_name="sensitive_field",
                    reason=f"Tool argument value for '{key}' contains sensitive data indicator",
                    agent_id=agent_id,
                    tool_name=tool_name,
                )
        return GuardrailResult(
            outcome=GuardrailOutcome.ALLOW,
            check_name="sensitive_field",
            reason="No sensitive field patterns detected",
            agent_id=agent_id,
            tool_name=tool_name,
        )

    def _check_injection(
        self, agent_id: str, tool_name: str, tool_args: dict[str, Any],
    ) -> GuardrailResult:
        """Detect obvious prompt/tool injection indicators in tool arguments."""
        for key, value in tool_args.items():
            if isinstance(value, str) and _INJECTION_PATTERNS.search(value):
                return GuardrailResult(
                    outcome=GuardrailOutcome.BLOCK,
                    check_name="injection_detection",
                    reason=f"Possible injection detected in argument '{key}'",
                    agent_id=agent_id,
                    tool_name=tool_name,
                )
        return GuardrailResult(
            outcome=GuardrailOutcome.ALLOW,
            check_name="injection_detection",
            reason="No injection patterns detected",
            agent_id=agent_id,
            tool_name=tool_name,
        )

    def _check_scope_authorization(
        self,
        agent_id: str,
        tool_name: str,
        agent_allowed_scopes: list[str] | None,
    ) -> GuardrailResult:
        """Check if the tool's data scope is within the agent's allowed scopes."""
        if not agent_allowed_scopes:
            return GuardrailResult(
                outcome=GuardrailOutcome.ALLOW,
                check_name="scope_authorization",
                reason="No scope restrictions defined for agent",
                agent_id=agent_id,
                tool_name=tool_name,
            )

        tool_scope = _TOOL_DATA_SCOPES.get(tool_name)
        if not tool_scope:
            return GuardrailResult(
                outcome=GuardrailOutcome.ALLOW,
                check_name="scope_authorization",
                reason=f"No data scope defined for tool '{tool_name}'",
                agent_id=agent_id,
                tool_name=tool_name,
            )

        for allowed in agent_allowed_scopes:
            if allowed == tool_scope:
                return GuardrailResult(
                    outcome=GuardrailOutcome.ALLOW,
                    check_name="scope_authorization",
                    reason=f"Tool scope '{tool_scope}' matches allowed scope '{allowed}'",
                    agent_id=agent_id,
                    tool_name=tool_name,
                )
            if allowed.endswith(".*"):
                prefix = allowed[:-2]
                if tool_scope.startswith(prefix):
                    return GuardrailResult(
                        outcome=GuardrailOutcome.ALLOW,
                        check_name="scope_authorization",
                        reason=f"Tool scope '{tool_scope}' matches wildcard scope '{allowed}'",
                        agent_id=agent_id,
                        tool_name=tool_name,
                    )

        return GuardrailResult(
            outcome=GuardrailOutcome.BLOCK,
            check_name="scope_authorization",
            reason=f"Tool '{tool_name}' requires scope '{tool_scope}' which is outside "
                   f"agent's allowed scopes {agent_allowed_scopes}",
            agent_id=agent_id,
            tool_name=tool_name,
        )

    def _check_tool_authorization(
        self,
        agent_id: str,
        tool_name: str,
        agent_allowed_tools: list[str],
    ) -> GuardrailResult:
        """Check if the tool is in the agent's allowed tool list."""
        if tool_name in agent_allowed_tools:
            return GuardrailResult(
                outcome=GuardrailOutcome.ALLOW,
                check_name="tool_authorization",
                reason=f"Tool '{tool_name}' is in agent's allowed tool set",
                agent_id=agent_id,
                tool_name=tool_name,
            )
        return GuardrailResult(
            outcome=GuardrailOutcome.BLOCK,
            check_name="tool_authorization",
            reason=f"Agent '{agent_id}' is not authorized for tool '{tool_name}'",
            agent_id=agent_id,
            tool_name=tool_name,
        )


# Module-level singleton
fleet_guardrails = AgentGuardrails()
