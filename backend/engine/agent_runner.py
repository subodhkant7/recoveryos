"""
Agent execution runner for RecoveryOS workflows.

Handles the autonomous Gemini ADK agent loop, prompt construction,
outcome verification, state transitions, and step-level cancellation checks.
Shared between API direct dispatch and asynchronous Worker event consumers.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.models.workflow import WorkflowState
from backend.models.events import EventType
from backend.persistence.workflow_store import BaseWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.agents.agent_factory import AgentFactory

logger = logging.getLogger("recoveryos.engine.agent_runner")


def build_agent_prompt(
    snapshot: dict[str, Any],
    customer: dict[str, Any],
    contract: dict[str, Any],
) -> str:
    """Build the initial prompt for the agent with full workflow context."""
    workflow = snapshot.get("workflow", {})
    workflow_id = workflow.get("workflow_id", "")

    completed_steps = [
        s for s in snapshot.get("steps", []) if s.get("status") == "COMPLETED"
    ]
    failed_steps = [
        s for s in snapshot.get("steps", []) if s.get("status") == "FAILED"
    ]
    outcomes = contract.get("required_outcomes", [])
    constraints = contract.get("constraints", [])
    prohibited = contract.get("prohibited_outcomes", [])

    prompt = f"""Execute the customer onboarding workflow for {customer.get('company_name', 'the customer')}.

WORKFLOW ID: {workflow_id}
CUSTOMER ID: {customer.get('customer_id', '')}
CUSTOMER NAME: {customer.get('full_name', '')}
EMAIL: {customer.get('email', '')}
REQUESTED PLAN: {customer.get('requested_plan', 'enterprise')}
BILLING CYCLE: {customer.get('billing_cycle', 'monthly')}
PREFERRED BILLING PROVIDER: {customer.get('preferred_billing_provider', 'stripe')}

OUTCOME CONTRACT — Required Outcomes:
"""

    for o in outcomes:
        verified = "✅ VERIFIED" if o.get("verified") else "❌ NOT VERIFIED"
        prompt += f"- {o['outcome_id']}: {o['description']} [{verified}]\n"

    if constraints:
        prompt += "\nCONSTRAINTS:\n"
        for c in constraints:
            prompt += f"- {c['constraint_id']}: {c['description']} (severity: {c.get('severity', 'hard')})\n"

    if prohibited:
        prompt += "\nPROHIBITED OUTCOMES:\n"
        for p in prohibited:
            desc = p.get("description", p) if isinstance(p, dict) else str(p)
            prompt += f"- ⛔ NEVER: {desc}\n"

    if completed_steps:
        prompt += "\nALREADY COMPLETED STEPS:\n"
        for s in completed_steps:
            prompt += f"- Step '{s.get('name', '')}' ({s.get('tool_name', '')}): COMPLETED\n"

    if failed_steps:
        prompt += "\nPREVIOUSLY FAILED STEPS (Need Recovery):\n"
        for s in failed_steps:
            prompt += f"- Step '{s.get('name', '')}' ({s.get('tool_name', '')}): FAILED — {s.get('error', 'unknown error')}\n"

    failures = snapshot.get("failures", [])
    if failures:
        prompt += "\nACTIVE FAILURE SIGNALS:\n"
        for f in failures:
            prompt += f"- Failure in '{f.get('component', '')}': {f.get('error_message', '')}\n"

    prompt += """
INSTRUCTIONS:
1. Review the required outcomes and determine which steps are still needed.
2. Execute each necessary step using available tools in the correct dependency order.
3. If an error occurs, adapt your plan to use alternative providers or safe paths.
4. After completing actions, verify each required outcome has been satisfied.
5. Provide concise reasoning before each tool call explaining why you chose this action.
"""
    return prompt


async def run_workflow_agent(
    workflow_id: str,
    store: BaseWorkflowStore,
    engine: WorkflowEngine,
    agent_factory: AgentFactory,
) -> dict[str, Any]:
    """
    Run the ADK agent loop for a workflow.

    Guarantees:
    - Safe state transition to EXECUTING.
    - Continuous cancellation check: halts immediately if state becomes ESCALATED or AWAITING_APPROVAL.
    - Outcome contract verification before transition to COMPLETED.
    - Transitions to UNKNOWN or RECOVERING on failures.
    """
    try:
        wf_data = await store.get_workflow(workflow_id)
        if not wf_data:
            return {"status": "NOT_FOUND", "workflow_id": workflow_id}

        # Terminal state guard
        if wf_data.get("state") in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value):
            return {
                "status": "TERMINAL_SKIPPED",
                "workflow_id": workflow_id,
                "state": wf_data.get("state"),
            }

        # Transition to EXECUTING if not already
        if wf_data.get("state") in (WorkflowState.CREATED.value, WorkflowState.UNKNOWN.value):
            await engine.transition(
                workflow_id,
                WorkflowState.EXECUTING,
                detail="Agent beginning autonomous execution",
                actor="taskmaster",
            )

        # In multi-process and containerized environments, auto-configure scenario failure injection
        scenario_name = wf_data.get("scenario")
        services = getattr(agent_factory, "services", getattr(agent_factory, "_services", None))
        injector = getattr(services, "failure_injector", getattr(services, "_injector", None)) if services else None
        if scenario_name and injector:
            try:
                from backend.simulation.scenarios import configure_demo_scenario
                configure_demo_scenario(injector, workflow_id, scenario_name)
            except Exception as e:
                logger.warning(f"Could not configure scenario '{scenario_name}' on runner: {e}")

        # In-flight cancellation check
        cur_wf = await store.get_workflow(workflow_id)
        if cur_wf and cur_wf.get("state") in (
            WorkflowState.ESCALATED.value,
            WorkflowState.AWAITING_APPROVAL.value,
        ):
            logger.info(f"Agent execution halted due to workflow state '{cur_wf.get('state')}'")
            return {"status": "HALTED", "workflow_id": workflow_id, "state": cur_wf.get("state")}

        # Build prompt from snapshot
        snapshot = await store.get_workflow_snapshot(workflow_id)
        if not snapshot:
            return {"status": "NOT_FOUND", "workflow_id": workflow_id}

        customer = wf_data.get("customer_data", {})
        contract = wf_data.get("contract", {})
        prompt = build_agent_prompt(snapshot, customer, contract)

        # Create orchestrator
        orchestrator = agent_factory.create_orchestrator()

        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        session_service = InMemorySessionService()
        runner = Runner(
            agent=orchestrator,
            app_name="recoveryos",
            session_service=session_service,
        )

        session = await session_service.create_session(
            app_name="recoveryos",
            user_id="system",
        )

        # Stream runner execution
        async for event in runner.run_async(
            session_id=session.id,
            user_id="system",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=prompt)],
            ),
        ):
            # Check for mid-execution operator cancellation
            interim_wf = await store.get_workflow(workflow_id)
            if interim_wf and interim_wf.get("state") in (
                WorkflowState.ESCALATED.value,
                WorkflowState.AWAITING_APPROVAL.value,
            ):
                logger.info(f"Halting agent runner: workflow transitioned to '{interim_wf.get('state')}'")
                return {
                    "status": "CANCELLED_MID_EXECUTION",
                    "workflow_id": workflow_id,
                    "state": interim_wf.get("state"),
                }

            # Record reasoning event
            if hasattr(event, "content") and event.content:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        await engine._record_event(
                            workflow_id=workflow_id,
                            event_type=EventType.AGENT_REASONING,
                            title="Agent Response",
                            detail=part.text[:500],
                            payload={"full_text": part.text},
                            actor=event.author if hasattr(event, "author") else "agent",
                        )

        # Post-execution verification gate
        wf_final = await store.get_workflow(workflow_id)
        if wf_final and wf_final.get("state") == WorkflowState.EXECUTING.value:
            await engine.transition(
                workflow_id,
                WorkflowState.VERIFYING,
                detail="Agent completed execution, verifying outcomes",
                actor="system",
            )

            # Re-fetch authoritative workflow to inspect latest verified contract
            wf_final = await store.get_workflow(workflow_id) or wf_final
            contract = wf_final.get("contract", {})
            all_verified = all(
                o.get("verified", False)
                for o in contract.get("required_outcomes", [])
            )

            if all_verified:
                await engine.transition(
                    workflow_id,
                    WorkflowState.COMPLETED,
                    detail="All outcomes independently verified",
                    actor="system",
                )
                return {"status": "COMPLETED", "workflow_id": workflow_id}
            else:
                unverified = [
                    o["outcome_id"]
                    for o in contract.get("required_outcomes", [])
                    if not o.get("verified", False)
                ]
                await engine.transition(
                    workflow_id,
                    WorkflowState.RECOVERING,
                    detail=f"Unverified outcomes: {unverified}",
                    actor="system",
                )
                return {"status": "RECOVERING", "workflow_id": workflow_id, "unverified": unverified}

        return {
            "status": wf_final.get("state") if wf_final else "UNKNOWN",
            "workflow_id": workflow_id,
        }

    except Exception as e:
        logger.error(f"Agent execution error on workflow '{workflow_id}': {e}", exc_info=True)
        await engine._record_event(
            workflow_id=workflow_id,
            event_type=EventType.STEP_FAILED,
            title="Agent Execution Error",
            detail=str(e),
            payload={"error_type": type(e).__name__},
            actor="system",
        )
        try:
            wf_cur = await store.get_workflow(workflow_id)
            if wf_cur and wf_cur.get("state") == WorkflowState.EXECUTING.value:
                await engine.transition(
                    workflow_id,
                    WorkflowState.UNKNOWN,
                    detail=f"Agent execution interrupted by error: {type(e).__name__}",
                    actor="system",
                )
        except Exception:
            pass
        return {"status": "ERROR", "workflow_id": workflow_id, "error": str(e)}
