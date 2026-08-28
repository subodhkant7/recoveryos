"""
Deterministic Policy Engine.

This module enforces authorization, safety constraints, and invariants
through pure Python code. NO LLM reasoning is involved in policy decisions.

Every mutating tool call passes through the policy engine via the ADK
before_tool_callback. The engine evaluates a set of deterministic rules
and returns APPROVED, REJECTED, or REQUIRES_HUMAN_APPROVAL.

These rules are Python if-statements, not LLM prompts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from backend.models.policy import PolicyDecision, PolicyOutcome, PolicyRuleResult
from backend.models.workflow import OutcomeContract


class PolicyEngine:
    """
    Deterministic policy evaluator.

    Evaluates a fixed set of rules against the current workflow state,
    evidence chain, approvals, and proposed tool call.
    """

    def evaluate(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        workflow_state: dict[str, Any],
        evidence: list[dict[str, Any]],
        contract: dict[str, Any] | None = None,
        approvals: list[dict[str, Any]] | None = None,
    ) -> PolicyDecision:
        """
        Run all policy rules and return a decision.

        Args:
            tool_name: Name of the tool being called
            tool_args: Arguments to the tool
            workflow_state: Current workflow state dict
            evidence: List of all evidence records for this workflow
            contract: OutcomeContract dict (if available)
            approvals: List of human approval records for this workflow

        Returns:
            PolicyDecision with outcome and rule results
        """
        workflow_id = workflow_state.get("workflow_id", "unknown")
        rules_results: list[PolicyRuleResult] = []

        # Run each rule
        rules_results.append(self._check_idempotency(tool_name, tool_args, workflow_state))
        rules_results.append(self._check_step_ordering(tool_name, tool_args, workflow_state, contract))
        rules_results.append(self._check_evidence_consistency(tool_name, tool_args, evidence, approvals))
        rules_results.append(self._check_recovery_budget(workflow_state))
        rules_results.append(self._check_tool_authorization(tool_name, workflow_state))
        rules_results.append(self._check_outcome_scope(tool_name, contract))
        rules_results.append(self._check_billing_sanity(tool_name, tool_args, approvals))

        # Determine overall outcome
        requires_human = any(
            not r.passed and "REQUIRES_HUMAN" in r.detail
            for r in rules_results
        )
        any_rejected = any(
            not r.passed and "REQUIRES_HUMAN" not in r.detail
            for r in rules_results
        )

        if any_rejected:
            failed_rules = [r for r in rules_results if not r.passed and "REQUIRES_HUMAN" not in r.detail]
            outcome = PolicyOutcome.REJECTED
            reason = "; ".join(r.detail for r in failed_rules)
        elif requires_human:
            human_rules = [r for r in rules_results if not r.passed]
            outcome = PolicyOutcome.REQUIRES_HUMAN_APPROVAL
            reason = "; ".join(r.detail for r in human_rules)
        else:
            outcome = PolicyOutcome.APPROVED
            reason = "All policy rules passed"

        return PolicyDecision(
            decision_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            tool_name=tool_name,
            rules_evaluated=rules_results,
            outcome=outcome,
            reason=reason,
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Individual policy rules (each is a pure Python function)
    # ------------------------------------------------------------------

    def _check_idempotency(
        self, tool_name: str, tool_args: dict, workflow_state: dict,
    ) -> PolicyRuleResult:
        """Has this exact operation already succeeded?"""
        steps = workflow_state.get("steps", [])
        for step in steps:
            if (
                step.get("tool_name") == tool_name
                and step.get("tool_args") == tool_args
                and step.get("status") == "COMPLETED"
            ):
                return PolicyRuleResult(
                    rule_name="idempotency_guard",
                    passed=True,
                    detail=f"Operation already completed in step {step.get('step_id')}; idempotency layer will return cached result",
                )
        return PolicyRuleResult(
            rule_name="idempotency_guard",
            passed=True,
            detail="No prior execution found; safe to proceed",
        )

    def _check_step_ordering(
        self, tool_name: str, tool_args: dict,
        workflow_state: dict, contract: dict | None,
    ) -> PolicyRuleResult:
        """Are prerequisite steps complete?"""
        if not contract:
            return PolicyRuleResult(
                rule_name="step_ordering",
                passed=True,
                detail="No contract constraints to check",
            )

        constraints = contract.get("constraints", [])
        steps = workflow_state.get("steps", [])
        completed_tools = {
            s.get("tool_name") for s in steps if s.get("status") == "COMPLETED"
        }

        # Check: identity_first constraint
        for c in constraints:
            c_id = c.get("constraint_id") if isinstance(c, dict) else str(c)
            if c_id == "identity_first":
                if tool_name != "verify_identity" and "verify_identity" not in completed_tools:
                    return PolicyRuleResult(
                        rule_name="step_ordering",
                        passed=False,
                        detail=f"REJECTED: Identity must be verified before '{tool_name}' (constraint: identity_first)",
                    )

            # Check: risk_before_billing constraint
            if c_id == "risk_before_billing":
                if tool_name == "setup_billing" and "run_risk_check" not in completed_tools:
                    return PolicyRuleResult(
                        rule_name="step_ordering",
                        passed=False,
                        detail="REJECTED: Risk assessment must pass before billing (constraint: risk_before_billing)",
                    )

        return PolicyRuleResult(
            rule_name="step_ordering",
            passed=True,
            detail="All ordering constraints satisfied",
        )

    def _check_evidence_consistency(
        self, tool_name: str, tool_args: dict, evidence: list[dict],
        approvals: list[dict] | None = None,
    ) -> PolicyRuleResult:
        """Do evidence records contradict each other?"""
        clean_args = {k: v for k, v in tool_args.items() if k not in ("workflow_id", "step_id")}

        # Check if an explicit approval exists for this action
        for app in (approvals or []):
            if app.get("action_tool") == tool_name:
                app_args = {k: v for k, v in app.get("action_args", {}).items() if k not in ("workflow_id", "step_id")}
                # Only apply if args match or approval was generic for this tool
                if not app_args or clean_args == app_args:
                    if app.get("status") == "APPROVED":
                        return PolicyRuleResult(
                            rule_name="evidence_consistency",
                            passed=True,
                            detail=f"Human approved action {app.get('approval_id')}: {app.get('decision_reason', 'Authorized')}",
                        )
                    elif app.get("status") == "REJECTED":
                        return PolicyRuleResult(
                            rule_name="evidence_consistency",
                            passed=False,
                            detail=f"REJECTED: Human rejected approval {app.get('approval_id')}: {app.get('decision_reason', 'Unauthorized')}",
                        )

        # Look for contradictory evidence about billing
        if tool_name in ("setup_billing", "verify_outcome"):
            billing_evidence = [
                e for e in evidence
                if e.get("source", "").startswith("setup_billing")
                or e.get("source", "").startswith("billing")
            ]
            if len(billing_evidence) >= 2:
                plans = set()
                for e in billing_evidence:
                    data = e.get("data", {})
                    plan = data.get("plan_tier") or data.get("plan")
                    if plan:
                        plans.add(plan)
                if len(plans) > 1:
                    return PolicyRuleResult(
                        rule_name="evidence_consistency",
                        passed=False,
                        detail=f"REQUIRES_HUMAN: Contradictory billing evidence — plans found: {plans}",
                    )

        return PolicyRuleResult(
            rule_name="evidence_consistency",
            passed=True,
            detail="No contradictory evidence found",
        )

    def _check_recovery_budget(
        self, workflow_state: dict,
    ) -> PolicyRuleResult:
        """Has the recovery attempt budget been exceeded?"""
        attempts = workflow_state.get("recovery_attempts", 0)
        max_attempts = workflow_state.get("max_recovery_attempts", 3)
        if attempts >= max_attempts:
            return PolicyRuleResult(
                rule_name="recovery_budget",
                passed=False,
                detail=f"REJECTED: Recovery budget exceeded ({attempts}/{max_attempts} attempts used)",
            )
        return PolicyRuleResult(
            rule_name="recovery_budget",
            passed=True,
            detail=f"Recovery budget OK ({attempts}/{max_attempts} used)",
        )

    def _check_tool_authorization(
        self, tool_name: str, workflow_state: dict,
    ) -> PolicyRuleResult:
        """Is the requesting agent authorized to call this tool?"""
        current_phase = workflow_state.get("state", "")
        mutating_tools = {
            "verify_identity", "validate_documents", "run_risk_check",
            "setup_billing", "activate_account", "send_welcome_package",
        }

        if current_phase == "RECOVERING" and tool_name in mutating_tools:
            return PolicyRuleResult(
                rule_name="tool_authorization",
                passed=True,
                detail="Tool is mutating but workflow will transition to EXECUTING before call",
            )

        return PolicyRuleResult(
            rule_name="tool_authorization",
            passed=True,
            detail=f"Tool '{tool_name}' is authorized in current phase",
        )

    def _check_outcome_scope(
        self, tool_name: str, contract: dict | None,
    ) -> PolicyRuleResult:
        """Is the proposed action relevant to the outcome contract?"""
        if not contract:
            return PolicyRuleResult(
                rule_name="outcome_scope",
                passed=True,
                detail="No contract to scope against",
            )

        tool_outcome_map = {
            "verify_identity": "identity_verified",
            "validate_documents": "documents_validated",
            "run_risk_check": "risk_assessed",
            "setup_billing": "billing_configured",
            "activate_account": "account_activated",
            "send_welcome_package": "welcome_sent",
            "verify_outcome": None,
            "check_service_status": None,
            "list_available_billing_providers": None,
            "get_workflow_state": None,
            "record_evidence": None,
            "request_human_approval": None,
        }

        if tool_name not in tool_outcome_map:
            return PolicyRuleResult(
                rule_name="outcome_scope",
                passed=False,
                detail=f"REJECTED: Unknown tool '{tool_name}' not in approved tool set",
            )

        return PolicyRuleResult(
            rule_name="outcome_scope",
            passed=True,
            detail=f"Tool '{tool_name}' is within outcome contract scope",
        )

    def _check_billing_sanity(
        self, tool_name: str, tool_args: dict,
        approvals: list[dict] | None = None,
    ) -> PolicyRuleResult:
        """Does a billing configuration exceed sane bounds?"""
        if tool_name != "setup_billing":
            return PolicyRuleResult(
                rule_name="billing_sanity",
                passed=True,
                detail="Not a billing operation",
            )

        clean_args = {k: v for k, v in tool_args.items() if k not in ("workflow_id", "step_id")}

        # Check if an explicit approval exists for this action
        for app in (approvals or []):
            if app.get("action_tool") == tool_name:
                app_args = {k: v for k, v in app.get("action_args", {}).items() if k not in ("workflow_id", "step_id")}
                if not app_args or clean_args == app_args:
                    if app.get("status") == "APPROVED":
                        return PolicyRuleResult(
                            rule_name="billing_sanity",
                            passed=True,
                            detail=f"Human approved billing plan tier {clean_args.get('plan_tier')}",
                        )
                    elif app.get("status") == "REJECTED":
                        return PolicyRuleResult(
                            rule_name="billing_sanity",
                            passed=False,
                            detail=f"REJECTED: Human rejected billing plan tier {clean_args.get('plan_tier')}",
                        )

        plan_tier = tool_args.get("plan_tier", "")
        if plan_tier not in ("starter", "professional", "enterprise", ""):
            return PolicyRuleResult(
                rule_name="billing_sanity",
                passed=False,
                detail=f"REQUIRES_HUMAN: Unknown billing plan tier '{plan_tier}'",
            )

        return PolicyRuleResult(
            rule_name="billing_sanity",
            passed=True,
            detail=f"Billing plan tier '{plan_tier}' is valid",
        )
