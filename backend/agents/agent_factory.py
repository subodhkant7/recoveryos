"""
ADK Agent Definitions.

Creates the two agents (Taskmaster and Recovery Specialist)
using Google ADK's LlmAgent, wires them with tools and the
before_tool_callback for policy enforcement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from backend.config import config
from backend.engine.policy_engine import PolicyEngine
from backend.engine.workflow_engine import WorkflowEngine
from backend.persistence.workflow_store import WorkflowStore
from backend.simulation.external_services import SimulatedServices
from backend.tools.onboarding.tools import OnboardingTools
from backend.models.events import EventType
from backend.models.workflow import WorkflowState
from backend.engine.idempotency import derive_idempotency_key


INSTRUCTIONS_DIR = Path(__file__).parent / "instructions"


def _load_instructions(filename: str) -> str:
    """Load agent instructions from a markdown file."""
    return (INSTRUCTIONS_DIR / filename).read_text()


def _build_before_tool_callback(
    policy_engine: PolicyEngine,
    store: WorkflowStore,
    engine: WorkflowEngine,
):
    """
    Build the ADK before_tool_callback for policy enforcement.

    This callback runs BEFORE any tool executes. It:
    1. Loads current workflow state
    2. Runs deterministic policy rules
    3. Returns None to allow execution, or a dict to block it

    This is the enforcement boundary between LLM reasoning
    and deterministic application policy.
    """

    async def before_tool_callback(
        tool: FunctionTool,
        args: dict[str, Any],
        tool_context: Any,
    ) -> dict | None:
        """
        Policy gate: runs before every tool call.

        Returns None to allow, or a dict to block with explanation.
        """
        tool_name = tool.name if hasattr(tool, 'name') else str(tool)

        # Read-only and planning tools skip mutating policy check
        read_only_tools = {
            "check_service_status",
            "list_available_billing_providers",
            "get_workflow_state",
            "verify_outcome",
            "submit_recovery_plan",
        }
        if tool_name in read_only_tools:
            return None  # Allow

        # Extract workflow_id from args
        workflow_id = args.get("workflow_id", "")
        if not workflow_id:
            return None  # No workflow context — allow (edge case)

        # Load current state
        wf_data = await store.get_workflow(workflow_id)
        if not wf_data:
            return None  # Workflow not found — let tool handle error

        # Load evidence, steps, and approvals
        evidence = await store.get_all_evidence(workflow_id)
        steps = await store.get_steps(workflow_id)
        approvals = await store.get_approvals(workflow_id)
        wf_data["steps"] = steps

        contract = wf_data.get("contract")

        # Evaluate policy
        decision = policy_engine.evaluate(
            tool_name=tool_name,
            tool_args=args,
            workflow_state=wf_data,
            evidence=[e for e in evidence],
            contract=contract,
            approvals=approvals,
        )

        # Record the policy decision as an event
        await engine._record_event(
            workflow_id=workflow_id,
            event_type=EventType.POLICY_DECISION,
            title=f"Policy: {decision.outcome.value} for {tool_name}",
            detail=decision.reason,
            payload={
                "decision_id": decision.decision_id,
                "tool_name": tool_name,
                "outcome": decision.outcome.value,
                "rules": [r.model_dump() for r in decision.rules_evaluated],
            },
            actor="policy_engine",
        )

        if decision.outcome.value == "APPROVED":
            return None  # Allow execution

        if decision.outcome.value == "REQUIRES_HUMAN_APPROVAL":
            # Deterministic deduplication key for approval request
            dedup_key = derive_idempotency_key(
                workflow_id=workflow_id,
                tool_name=tool_name,
                target_entity_id=args.get("customer_id", ""),
                parameters=args,
            )

            # Check if an identical pending approval already exists
            existing = await store.get_pending_approval_by_dedup(workflow_id, dedup_key)
            if existing:
                approval_id = existing["approval_id"]
            else:
                from backend.models.approval import HumanApproval, ApprovalStatus
                clean_args = {k: v for k, v in args.items() if k not in ("workflow_id", "step_id")}
                human_rule = next(
                    (r.rule_name for r in decision.rules_evaluated if not r.passed and "REQUIRES_HUMAN" in r.detail),
                    "policy_rule",
                )
                approval = HumanApproval(
                    workflow_id=workflow_id,
                    action_tool=tool_name,
                    action_args=clean_args,
                    policy_rule=human_rule,
                    reason=decision.reason,
                    title=f"Approval Required: {tool_name}",
                    description=decision.reason,
                    dedup_key=dedup_key,
                    status=ApprovalStatus.PENDING,
                    evidence_ids=[e["evidence_id"] for e in evidence],
                )
                await store.save_approval(workflow_id, approval.model_dump(mode="json"))
                approval_id = approval.approval_id

                await engine._record_event(
                    workflow_id=workflow_id,
                    event_type=EventType.APPROVAL_REQUESTED,
                    title=f"Approval Requested: {tool_name}",
                    detail=decision.reason,
                    payload={
                        "approval_id": approval_id,
                        "tool_name": tool_name,
                        "action_args": clean_args,
                        "policy_rule": human_rule,
                    },
                    actor="policy_engine",
                )

            # Transition workflow to AWAITING_APPROVAL if not already
            if wf_data.get("state") != WorkflowState.AWAITING_APPROVAL.value:
                await engine.transition(
                    workflow_id,
                    WorkflowState.AWAITING_APPROVAL,
                    detail=f"Paused: {decision.reason}",
                    actor="policy_engine",
                )

            return {
                "status": "blocked",
                "reason": decision.reason,
                "action_required": "human_approval",
                "approval_id": approval_id,
                "decision_id": decision.decision_id,
            }

        # REJECTED
        return {
            "status": "blocked",
            "reason": decision.reason,
            "action_required": "rejected",
            "decision_id": decision.decision_id,
        }

    return before_tool_callback


def _get_agent_model():
    """Get the ADK Gemini model configured with the application's runtime API key and resilience layer."""
    from backend.llm.resilience import ResilientGemini
    client_kwargs = {"api_key": config.google_api_key} if config.google_api_key else None
    return ResilientGemini(model=config.gemini_model, client_kwargs=client_kwargs)


class AgentFactory:
    """
    Factory for creating configured ADK agents.

    Wires together all dependencies: tools, policy engine,
    workflow engine, persistence, and simulated services.
    """

    def __init__(
        self,
        store: WorkflowStore,
        engine: WorkflowEngine,
        services: SimulatedServices,
        policy_engine: PolicyEngine,
    ):
        self._store = store
        self._engine = engine
        self._services = services
        self._policy_engine = policy_engine
        self._tools = OnboardingTools(services, store, engine)

    @property
    def services(self) -> SimulatedServices:
        return self._services

    @property
    def tools(self) -> OnboardingTools:
        return self._tools

    def create_taskmaster(self) -> LlmAgent:
        """Create the Taskmaster agent with all onboarding tools."""
        callback = _build_before_tool_callback(
            self._policy_engine, self._store, self._engine
        )

        return LlmAgent(
            name="taskmaster",
            model=_get_agent_model(),
            instruction=_load_instructions("taskmaster.md"),
            tools=[
                # Mutating tools
                self._tools.verify_identity,
                self._tools.validate_documents,
                self._tools.run_risk_check,
                self._tools.setup_billing,
                self._tools.activate_account,
                self._tools.send_welcome_package,
                # Diagnostic tools (read-only)
                self._tools.check_service_status,
                self._tools.list_available_billing_providers,
                self._tools.get_workflow_state,
                # Verification tools
                self._tools.verify_outcome,
            ],
            before_tool_callback=callback,
        )

    def create_recovery_specialist(self) -> LlmAgent:
        """
        Create the Recovery Specialist agent.

        This agent has READ-ONLY tools only. It cannot mutate
        external state. It diagnoses failures and proposes recovery plans.
        """
        return LlmAgent(
            name="recovery_specialist",
            model=_get_agent_model(),
            instruction=_load_instructions("recovery_specialist.md"),
            tools=[
                # Diagnostic and recovery planning tools — no mutating tools
                self._tools.check_service_status,
                self._tools.list_available_billing_providers,
                self._tools.get_workflow_state,
                self._tools.submit_recovery_plan,
            ],
        )

    def create_orchestrator(self) -> LlmAgent:
        """
        Create the top-level orchestrator agent.

        Delegates to Taskmaster for normal execution and
        Recovery Specialist for diagnosis/recovery planning.
        """
        taskmaster = self.create_taskmaster()
        recovery = self.create_recovery_specialist()

        return LlmAgent(
            name="recoveryos_orchestrator",
            model=_get_agent_model(),
            instruction=(
                "You are the RecoveryOS orchestrator. You coordinate the "
                "customer onboarding workflow.\n\n"
                "1. Delegate execution tasks to the 'taskmaster' agent.\n"
                "2. If the taskmaster reports a failure, delegate diagnosis to "
                "the 'recovery_specialist' agent.\n"
                "3. Once recovery_specialist proposes a plan, relay it back to "
                "the taskmaster for execution.\n"
                "4. Continue until all outcomes are verified or the workflow "
                "is escalated.\n\n"
                "You manage the conversation flow between agents. You do not "
                "call tools directly."
            ),
            sub_agents=[taskmaster, recovery],
        )
