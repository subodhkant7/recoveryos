"""
Workflow Engine.

Manages workflow lifecycle: state transitions, event recording,
and workflow resumability. This is deterministic application code
that enforces invariants — not agent reasoning.

The engine does NOT decide what steps to run. The agent does that.
The engine only validates state transitions and records events.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from backend.models.workflow import (
    Workflow,
    WorkflowState,
    VALID_TRANSITIONS,
    normalize_contract,
    normalize_customer_data,
)
from backend.models.events import EventType, WorkflowEvent
from backend.persistence.workflow_store import WorkflowStore


class InvalidTransitionError(Exception):
    """Raised when a workflow state transition is not valid."""

    pass


class WorkflowEngine:
    """
    Manages workflow lifecycle transitions and event recording.

    Guarantees:
    - State transitions are always valid (enforced by VALID_TRANSITIONS)
    - Every transition is recorded as an immutable event
    - Workflow state is persisted before any tool execution
    """

    def __init__(self, store: WorkflowStore):
        self._store = store

    async def create_workflow(
        self,
        name: str,
        scenario: str,
        customer_data: dict[str, Any],
        contract_data: dict[str, Any],
        workflow_id: str | None = None,
        tenant_id: str = "tenant-default",
    ) -> dict[str, Any]:
        """
        Create a new workflow and persist it.

        Returns the workflow dict (serialized Workflow model).
        """
        norm_customer = normalize_customer_data(customer_data)
        norm_contract = normalize_contract(contract_data, workflow_id=workflow_id or "")
        wf_id = workflow_id or norm_contract.get("workflow_id") or str(uuid.uuid4())
        norm_contract["workflow_id"] = wf_id

        workflow = Workflow(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            name=name,
            scenario=scenario,
            customer_data=norm_customer,
        )
        workflow.contract = None  # Will store norm_contract separately
        wf_data = workflow.model_dump(mode="json")
        wf_data["contract"] = norm_contract

        await self._store.save_workflow(wf_data)
        await self._record_event(
            workflow_id=workflow.workflow_id,
            event_type=EventType.STATE_CHANGE,
            title="Workflow Created",
            detail=f"Workflow '{name}' created for scenario '{scenario}'",
            payload={"state": WorkflowState.CREATED.value, "customer_data": norm_customer},
            actor="system",
        )
        return wf_data

    async def transition(
        self,
        workflow_id: str,
        new_state: WorkflowState,
        detail: str = "",
        actor: str = "system",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """
        Transition a workflow to a new state.

        Raises InvalidTransitionError if the transition is not valid.
        Raises StaleWorkflowStateError if OCC version check fails.
        Records an immutable event for the transition.
        """
        wf_data = await self._store.get_workflow(workflow_id)
        if not wf_data:
            raise ValueError(f"Workflow {workflow_id} not found")

        current_state = WorkflowState(wf_data["state"])

        if new_state not in VALID_TRANSITIONS.get(current_state, set()):
            raise InvalidTransitionError(
                f"Cannot transition from {current_state.value} to {new_state.value}. "
                f"Valid transitions: {[s.value for s in VALID_TRANSITIONS.get(current_state, set())]}"
            )

        old_state = current_state.value
        wf_data["state"] = new_state.value
        wf_data["updated_at"] = datetime.now(timezone.utc).isoformat()

        if new_state == WorkflowState.COMPLETED:
            wf_data["completed_at"] = datetime.now(timezone.utc).isoformat()

        if new_state == WorkflowState.RECOVERING:
            wf_data["recovery_attempts"] = wf_data.get("recovery_attempts", 0) + 1

        expected_ver = expected_version if expected_version is not None else wf_data.get("version", 1)
        await self._store.save_workflow(wf_data, expected_version=expected_ver)
        await self._record_event(
            workflow_id=workflow_id,
            event_type=EventType.STATE_CHANGE,
            title=f"State: {old_state} → {new_state.value}",
            detail=detail or f"Workflow transitioned to {new_state.value}",
            payload={"from_state": old_state, "to_state": new_state.value},
            actor=actor,
        )
        return await self._store.get_workflow(workflow_id) or wf_data

    async def reconcile_interrupted_workflow(
        self, workflow_id: str, services: Any
    ) -> dict[str, Any] | None:
        """
        Reconcile an interrupted or UNKNOWN workflow against external ground truth.
        Inspects pending/running steps and authoritative external records.
        """
        wf_data = await self._store.get_workflow(workflow_id)
        if not wf_data:
            return None

        current_state = wf_data.get("state")
        steps = await self._store.get_steps(workflow_id)
        for step in steps:
            if isinstance(step, dict) and step.get("status") in ("RUNNING", "PENDING"):
                step_id = step.get("step_id")
                if not step_id:
                    continue
                tool_name = step.get("tool_name", "")
                idem_key = step.get("idempotency_key", "")
                tool_args = step.get("tool_args", {}) if isinstance(step.get("tool_args"), dict) else {}
                cust_id = tool_args.get("customer_id", "")
                kwargs_clean = {k: v for k, v in tool_args.items() if k != "customer_id"}

                # Query external service ground truth
                external_record = await services.check_external_mutation(
                    tool_name=tool_name,
                    idempotency_key=idem_key,
                    customer_id=cust_id,
                    **kwargs_clean,
                )
                if external_record is not None:
                    # Side-effect succeeded externally
                    await self.record_step_completed(workflow_id, step_id, result=external_record)
                    idem_rec = {
                        "idempotency_key": idem_key,
                        "workflow_id": workflow_id,
                        "tool_name": tool_name,
                        "target_entity_id": cust_id,
                        "parameters": tool_args,
                        "status": "SUCCEEDED",
                        "result": external_record,
                        "step_id": step_id,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    await self._store.save_idempotency_record(idem_key, idem_rec)
                else:
                    # Side-effect was never completed externally
                    await self.record_step_failed(
                        workflow_id, step_id, error="Step interrupted by process restart before external completion"
                    )
                    idem_rec = {
                        "idempotency_key": idem_key,
                        "workflow_id": workflow_id,
                        "tool_name": tool_name,
                        "target_entity_id": cust_id,
                        "parameters": tool_args,
                        "status": "FAILED",
                        "error": "Process restarted before external mutation",
                        "step_id": step_id,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    await self._store.save_idempotency_record(idem_key, idem_rec)

        if current_state == WorkflowState.UNKNOWN.value:
            # Reconciled out of UNKNOWN
            return await self.transition(
                workflow_id,
                WorkflowState.EXECUTING,
                detail="Reconciliation completed: workflow resumed from UNKNOWN state",
                actor="system",
            )
        return await self._store.get_workflow(workflow_id)

    async def record_step_started(
        self, workflow_id: str, step_data: dict[str, Any],
    ) -> None:
        """Record that a step has started executing."""
        step_data["status"] = "RUNNING"
        step_data["started_at"] = datetime.now(timezone.utc).isoformat()
        step_data["attempt_count"] = step_data.get("attempt_count", 0) + 1
        await self._store.save_step(workflow_id, step_data)
        await self._record_event(
            workflow_id=workflow_id,
            event_type=EventType.STEP_STARTED,
            title=f"Step Started: {step_data.get('name', '')}",
            detail=f"Tool: {step_data.get('tool_name', '')}",
            payload={"step_id": step_data["step_id"], "tool_name": step_data.get("tool_name", "")},
            actor="taskmaster",
        )

    async def record_step_completed(
        self, workflow_id: str, step_id: str,
        result: dict[str, Any], evidence_id: str | None = None,
    ) -> None:
        """Record that a step completed successfully."""
        step_data = await self._store.get_step(workflow_id, step_id)
        if step_data:
            step_data["status"] = "COMPLETED"
            step_data["result"] = result
            step_data["evidence_id"] = evidence_id
            step_data["completed_at"] = datetime.now(timezone.utc).isoformat()
            await self._store.save_step(workflow_id, step_data)
        await self._record_event(
            workflow_id=workflow_id,
            event_type=EventType.STEP_COMPLETED,
            title=f"Step Completed: {step_data.get('name', '') if step_data else step_id}",
            detail=f"Result: {result.get('status', '')}",
            payload={"step_id": step_id, "result": result},
            actor="taskmaster",
        )

    async def record_step_failed(
        self, workflow_id: str, step_id: str,
        error: str, failure_data: dict[str, Any] | None = None,
    ) -> None:
        """Record that a step failed."""
        step_data = await self._store.get_step(workflow_id, step_id)
        if step_data:
            step_data["status"] = "FAILED"
            step_data["error"] = error
            step_data["completed_at"] = datetime.now(timezone.utc).isoformat()
            await self._store.save_step(workflow_id, step_data)
        if failure_data:
            await self._store.save_failure(workflow_id, failure_data)
        await self._record_event(
            workflow_id=workflow_id,
            event_type=EventType.STEP_FAILED,
            title=f"Step Failed: {step_data.get('name', '') if step_data else step_id}",
            detail=error,
            payload={"step_id": step_id, "error": error},
            actor="taskmaster",
        )

    async def _record_event(
        self,
        workflow_id: str,
        event_type: EventType,
        title: str,
        detail: str = "",
        payload: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> None:
        """Record an immutable workflow event."""
        event = WorkflowEvent(
            workflow_id=workflow_id,
            event_type=event_type,
            title=title,
            detail=detail,
            payload=payload or {},
            actor=actor,
        )
        event_dict = event.model_dump(mode="json")
        await self._store.append_event(workflow_id, event_dict)
        try:
            from backend.events.broadcast import event_broadcaster
            await event_broadcaster.broadcast(workflow_id, event_dict)
        except Exception:
            pass
