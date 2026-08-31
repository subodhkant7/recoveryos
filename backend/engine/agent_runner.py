"""
Agent execution runner for RecoveryOS workflows.

Handles the autonomous Gemini ADK agent loop, prompt construction,
outcome verification, state transitions, and step-level cancellation checks.
Shared between API direct dispatch and asynchronous Worker event consumers.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from backend.models.workflow import (
    WorkflowState,
    normalize_contract,
    normalize_customer_data,
    normalize_workflow_snapshot,
)
from backend.models.events import EventType
from backend.persistence.workflow_store import BaseWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.agents.agent_factory import AgentFactory
from backend.simulation.failure_injector import CrashBeforePersistenceError

logger = logging.getLogger("recoveryos.engine.agent_runner")


def build_agent_prompt(
    snapshot: dict[str, Any],
    customer: dict[str, Any],
    contract: dict[str, Any],
) -> str:
    """Build the initial prompt for the agent with full workflow context."""
    snapshot = normalize_workflow_snapshot(snapshot)
    customer = normalize_customer_data(customer)
    contract = normalize_contract(contract)

    workflow = snapshot.get("workflow", {})
    workflow_id = workflow.get("workflow_id", "")

    completed_steps = [
        s for s in snapshot.get("steps", []) if isinstance(s, dict) and s.get("status") == "COMPLETED"
    ]
    failed_steps = [
        s for s in snapshot.get("steps", []) if isinstance(s, dict) and s.get("status") == "FAILED"
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
        if isinstance(o, dict):
            o_id = o.get("outcome_id", "")
            o_desc = o.get("description", "")
            is_ver = bool(o.get("verified", False))
        else:
            o_id = str(o)
            o_desc = str(o)
            is_ver = False
        verified = "✅ VERIFIED" if is_ver else "❌ NOT VERIFIED"
        prompt += f"- {o_id}: {o_desc} [{verified}]\n"

    if constraints:
        prompt += "\nCONSTRAINTS:\n"
        for c in constraints:
            if isinstance(c, dict):
                c_id = c.get("constraint_id", "")
                c_desc = c.get("description", "")
                c_sev = c.get("severity", "hard")
            else:
                c_id = str(c)
                c_desc = str(c)
                c_sev = "hard"
            prompt += f"- {c_id}: {c_desc} (severity: {c_sev})\n"

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

    failures = [f for f in snapshot.get("failures", []) if isinstance(f, dict)]
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
    services: Any | None = None
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
        if wf_data.get("state") in (
            WorkflowState.CREATED.value,
            WorkflowState.UNKNOWN.value,
            WorkflowState.RECOVERING.value,
        ):
            await engine.transition(
                workflow_id,
                WorkflowState.EXECUTING,
                detail="Agent beginning autonomous execution",
                actor="taskmaster",
            )

        # Initialize Fleet Observability trace & record Orchestrator dispatch
        try:
            from backend.fleet.observability import fleet_tracer
            fleet_tracer.start_trace(workflow_id)
            fleet_tracer.record_event(
                workflow_id=workflow_id,
                agent_id="orchestrator",
                event_type="WORKFLOW_DISPATCH",
                detail=f"Orchestrator dispatched workflow for scenario '{wf_data.get('scenario', 'default')}'",
                metadata={"tenant_id": wf_data.get("tenant_id", "tenant-default"), "scenario": wf_data.get("scenario")},
            )
        except Exception:
            pass

        # In multi-process and containerized environments, auto-configure scenario failure injection
        scenario_name = wf_data.get("scenario")
        services = getattr(agent_factory, "services", getattr(agent_factory, "_services", None))
        injector = getattr(services, "failure_injector", getattr(services, "_injector", None)) if services else None
        if scenario_name and injector:
            try:
                from backend.simulation.scenarios import configure_demo_scenario
                configure_demo_scenario(injector, workflow_id, scenario_name, services=services)
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
            contract = normalize_contract(wf_final.get("contract", {}), workflow_id=workflow_id)
            req_outcomes = contract.get("required_outcomes", [])
            all_verified = all(
                (o.get("verified", False) if isinstance(o, dict) else False)
                for o in req_outcomes
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
                    (o.get("outcome_id") if isinstance(o, dict) else str(o))
                    for o in req_outcomes
                    if not (o.get("verified", False) if isinstance(o, dict) else False)
                ]
                await engine.transition(
                    workflow_id,
                    WorkflowState.RECOVERING,
                    detail=f"Unverified outcomes: {unverified}",
                    actor="system",
                )
                return {
                    "status": "RECOVERING",
                    "workflow_id": workflow_id,
                    "unverified": unverified,
                    "needs_redispatch": True,
                }

        return {
            "status": wf_final.get("state") if wf_final else "UNKNOWN",
            "workflow_id": workflow_id,
        }

    except Exception as e:
        err_msg = str(e) or type(e).__name__
        tb_str = traceback.format_exc()
        logger.error(f"Agent execution error on workflow '{workflow_id}': {type(e).__name__}: {err_msg}\n{tb_str}")
        is_interruption = isinstance(e, CrashBeforePersistenceError)
        await engine._record_event(
            workflow_id=workflow_id,
            event_type=EventType.STEP_FAILED,
            title=(
                "Worker Interruption After External Success"
                if is_interruption
                else "Agent Execution Error"
            ),
            detail=err_msg,
            payload={
                "error_type": type(e).__name__,
                "error_message": err_msg,
                "workflow_id": workflow_id,
                "requires_reconciliation": is_interruption,
            },
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

            # A simulated interruption models the dangerous boundary where an
            # external side effect can succeed before the worker persists its
            # local completion record. Reconcile against authoritative service
            # state first, then use the normal bounded recovery path.
            if is_interruption and services is not None:
                reconciled = await engine.reconcile_interrupted_workflow(
                    workflow_id, services
                )
                if reconciled and reconciled.get("state") == WorkflowState.EXECUTING.value:
                    await engine.transition(
                        workflow_id,
                        WorkflowState.RECOVERING,
                        detail="Worker interruption reconciled; dispatching bounded recovery",
                        actor="system",
                    )
                    await engine._record_event(
                        workflow_id=workflow_id,
                        event_type=EventType.WORKFLOW_RESUMED,
                        title="Worker Recovery Redispatched",
                        detail="External mutation reconciled before autonomous retry",
                        payload={"reconciled_after_interruption": True},
                        actor="system",
                    )
                    return {
                        "status": "RECOVERING",
                        "workflow_id": workflow_id,
                        "needs_redispatch": True,
                        "reconciled_after_interruption": True,
                    }
        except Exception:
            logger.exception(
                "Unable to reconcile interrupted workflow '%s'", workflow_id
            )
        return {"status": "ERROR", "workflow_id": workflow_id, "error": err_msg}
