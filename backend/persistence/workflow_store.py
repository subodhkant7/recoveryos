"""
Persistence layer for RecoveryOS.

Provides a unified interface for workflow state persistence:
- BaseWorkflowStore: Abstract base interface
- InMemoryWorkflowStore: In-memory store with Optimistic Concurrency Control (OCC) and Operation Claiming
- FirestoreWorkflowStore: Google Cloud Firestore implementation with transactions & batch writes
- create_workflow_store: Factory selecting backend via configuration
"""

from __future__ import annotations

import asyncio
import copy
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Any

from backend.config import config
from backend.models.idempotency import OperationStatus, OperationClaim


class StaleWorkflowStateError(Exception):
    """Raised when an update fails due to an optimistic concurrency control (OCC) version conflict."""
    pass


class BaseWorkflowStore(ABC):
    """Abstract base persistence interface for RecoveryOS workflows."""

    @abstractmethod
    def get_lock(self, key: str) -> asyncio.Lock:
        """Get or create a concurrency lock for a given idempotency key."""
        pass

    # ------------------------------------------------------------------
    # Workflow CRUD
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_workflow(
        self, workflow_data: dict[str, Any], expected_version: int | None = None
    ) -> None:
        """
        Save or update a workflow document.
        If expected_version is provided, enforces optimistic concurrency control (OCC).
        """
        pass

    @abstractmethod
    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """Retrieve a workflow document by ID."""
        pass

    @abstractmethod
    async def list_workflows(
        self,
        tenant_id: str | None = None,
        state: str | None = None,
        scenario: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List workflow documents with optional filtering and pagination."""
        pass

    @abstractmethod
    async def count_workflows(
        self,
        tenant_id: str | None = None,
        state: str | None = None,
    ) -> int:
        """Count workflow documents matching criteria."""
        pass

    @abstractmethod
    async def get_incomplete_workflows(self) -> list[dict[str, Any]]:
        """Find workflows not in terminal states (COMPLETED, ESCALATED)."""
        pass

    # ------------------------------------------------------------------
    # Audit Events
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_audit_event(self, audit_data: dict[str, Any]) -> None:
        """Save an immutable security/operator audit event."""
        pass

    @abstractmethod
    async def list_audit_events(
        self,
        tenant_id: str | None = None,
        workflow_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List audit events with optional filtering and pagination."""
        pass

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_step(self, workflow_id: str, step_data: dict[str, Any]) -> None:
        """Save or update a step document."""
        pass

    @abstractmethod
    async def get_step(self, workflow_id: str, step_id: str) -> dict[str, Any] | None:
        """Retrieve a step by ID."""
        pass

    @abstractmethod
    async def get_steps(self, workflow_id: str) -> list[dict[str, Any]]:
        """Retrieve all steps for a workflow."""
        pass

    # ------------------------------------------------------------------
    # Events (append-only)
    # ------------------------------------------------------------------

    @abstractmethod
    async def append_event(self, workflow_id: str, event_data: dict[str, Any]) -> None:
        """Append an event to the workflow timeline."""
        pass

    @abstractmethod
    async def get_events(self, workflow_id: str) -> list[dict[str, Any]]:
        """Retrieve all timeline events for a workflow in chronological order."""
        pass

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_evidence(self, workflow_id: str, evidence_data: dict[str, Any]) -> None:
        """Save an immutable evidence record."""
        pass

    @abstractmethod
    async def get_evidence(self, workflow_id: str, evidence_id: str) -> dict[str, Any] | None:
        """Retrieve an evidence record by ID."""
        pass

    @abstractmethod
    async def get_all_evidence(self, workflow_id: str) -> list[dict[str, Any]]:
        """Retrieve all evidence records for a workflow."""
        pass

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_failure(self, workflow_id: str, failure_data: dict[str, Any]) -> None:
        """Save a diagnosed or raw failure record."""
        pass

    @abstractmethod
    async def get_failure(self, workflow_id: str, failure_id: str) -> dict[str, Any] | None:
        """Retrieve a failure record by ID."""
        pass

    @abstractmethod
    async def get_failures(self, workflow_id: str) -> list[dict[str, Any]]:
        """Retrieve all failure records for a workflow."""
        pass

    # ------------------------------------------------------------------
    # Recovery Plans
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_recovery_plan(self, workflow_id: str, plan_data: dict[str, Any]) -> None:
        """Save a structured RecoveryPlan."""
        pass

    @abstractmethod
    async def get_recovery_plan(self, workflow_id: str, plan_id: str) -> dict[str, Any] | None:
        """Retrieve a recovery plan by ID."""
        pass

    @abstractmethod
    async def get_recovery_plans(self, workflow_id: str) -> list[dict[str, Any]]:
        """Retrieve all recovery plans for a workflow."""
        pass

    @abstractmethod
    async def get_active_recovery_plan(
        self, workflow_id: str, target_outcome_id: str | None = None
    ) -> dict[str, Any] | None:
        """Retrieve the latest active (PROPOSED, APPROVED, EXECUTING) recovery plan."""
        pass

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_approval(self, workflow_id: str, approval_data: dict[str, Any]) -> None:
        """Save a HumanApproval record."""
        pass

    @abstractmethod
    async def get_approval(self, workflow_id: str, approval_id: str) -> dict[str, Any] | None:
        """Retrieve a human approval record by ID."""
        pass

    @abstractmethod
    async def get_approvals(self, workflow_id: str) -> list[dict[str, Any]]:
        """Retrieve all approvals for a workflow."""
        pass

    @abstractmethod
    async def get_pending_approvals(self, workflow_id: str) -> list[dict[str, Any]]:
        """Retrieve all pending approvals for a workflow."""
        pass

    @abstractmethod
    async def get_pending_approval_by_dedup(
        self, workflow_id: str, dedup_key: str
    ) -> dict[str, Any] | None:
        """Retrieve a pending approval matching a deduplication key."""
        pass

    @abstractmethod
    async def get_approved_action(
        self, workflow_id: str, action_tool: str, action_args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Retrieve an approved action explicitly authorizing a tool and argument set."""
        pass

    # ------------------------------------------------------------------
    # Idempotency Records
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_idempotency_record(self, key: str) -> dict[str, Any] | None:
        """Retrieve a canonical idempotency record."""
        pass

    @abstractmethod
    async def save_idempotency_record(self, key: str, record_data: dict[str, Any]) -> None:
        """Save or update a canonical idempotency record."""
        pass

    # ------------------------------------------------------------------
    # Distributed Operation Claims (Multi-Worker Coordination)
    # ------------------------------------------------------------------

    @abstractmethod
    async def claim_operation(
        self,
        idempotency_key: str,
        workflow_id: str,
        tool_name: str,
        target_entity_id: str,
        parameters: dict[str, Any],
        worker_id: str,
        lease_seconds: int = 60,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Attempt to transactionally acquire a lease/claim on an operation.
        Returns (acquired: bool, operation_claim_dict: dict).
        """
        pass

    @abstractmethod
    async def complete_operation(
        self,
        idempotency_key: str,
        result: dict[str, Any],
        worker_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Mark an operation as COMPLETED with cached result."""
        pass

    @abstractmethod
    async def fail_operation(
        self,
        idempotency_key: str,
        error: str,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark an operation as FAILED."""
        pass

    @abstractmethod
    async def get_operation(self, idempotency_key: str) -> dict[str, Any] | None:
        """Retrieve the operation claim by key."""
        pass

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_workflow_snapshot(self, workflow_id: str) -> dict[str, Any] | None:
        """Load complete aggregated workflow snapshot."""
        pass


class InMemoryWorkflowStore(BaseWorkflowStore):
    """
    In-memory implementation of BaseWorkflowStore with Optimistic Concurrency Control (OCC)
    and distributed operation claim coordination.
    """

    def __init__(self, shared_data: dict[str, Any] | None = None) -> None:
        if shared_data is not None:
            self._workflows = shared_data["workflows"]
            self._steps = shared_data["steps"]
            self._events = shared_data["events"]
            self._evidence = shared_data["evidence"]
            self._failures = shared_data["failures"]
            self._recovery_plans = shared_data["recovery_plans"]
            self._approvals = shared_data["approvals"]
            self._idempotency = shared_data["idempotency"]
            self._operations = shared_data.get("operations", {})
            self._audit_events = shared_data.get("audit_events", [])
        else:
            self._workflows: dict[str, dict[str, Any]] = {}
            self._steps: dict[str, dict[str, dict[str, Any]]] = {}
            self._events: dict[str, list[dict[str, Any]]] = {}
            self._evidence: dict[str, dict[str, dict[str, Any]]] = {}
            self._failures: dict[str, dict[str, dict[str, Any]]] = {}
            self._recovery_plans: dict[str, dict[str, dict[str, Any]]] = {}
            self._approvals: dict[str, dict[str, dict[str, Any]]] = {}
            self._idempotency: dict[str, dict[str, Any]] = {}
            self._operations: dict[str, dict[str, Any]] = {}
            self._audit_events: list[dict[str, Any]] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def export_state(self) -> dict[str, Any]:
        """Export raw state dictionaries to simulate persistent storage snapshot across restarts."""
        return {
            "workflows": copy.deepcopy(self._workflows),
            "steps": copy.deepcopy(self._steps),
            "events": copy.deepcopy(self._events),
            "evidence": copy.deepcopy(self._evidence),
            "failures": copy.deepcopy(self._failures),
            "recovery_plans": copy.deepcopy(self._recovery_plans),
            "approvals": copy.deepcopy(self._approvals),
            "idempotency": copy.deepcopy(self._idempotency),
            "operations": copy.deepcopy(self._operations),
            "audit_events": copy.deepcopy(self._audit_events),
        }

    async def save_workflow(
        self, workflow_data: dict[str, Any], expected_version: int | None = None
    ) -> None:
        async with self._global_lock:
            wf_id = workflow_data["workflow_id"]
            current = self._workflows.get(wf_id)
            current_version = current.get("version", 1) if current else 0

            if expected_version is not None and current is not None:
                if current_version != expected_version:
                    raise StaleWorkflowStateError(
                        f"OCC Conflict: Workflow '{wf_id}' is at version {current_version}, expected {expected_version}"
                    )

            new_version = current_version + 1 if current else workflow_data.get("version", 1)
            data = copy.deepcopy(workflow_data)
            data["version"] = new_version
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._workflows[wf_id] = data

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        data = self._workflows.get(workflow_id)
        return copy.deepcopy(data) if data else None

    async def list_workflows(
        self,
        tenant_id: str | None = None,
        state: str | None = None,
        scenario: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for w in self._workflows.values():
            if tenant_id and w.get("tenant_id", "tenant-default") != tenant_id:
                continue
            if state and w.get("state") != state:
                continue
            if scenario and w.get("scenario") != scenario:
                continue
            results.append(copy.deepcopy(w))

        # Sort by updated_at or created_at desc if available
        results.sort(
            key=lambda x: x.get("updated_at") or x.get("created_at") or "",
            reverse=True,
        )

        if offset > 0:
            results = results[offset:]
        if limit is not None and limit > 0:
            results = results[:limit]
        return results

    async def count_workflows(
        self,
        tenant_id: str | None = None,
        state: str | None = None,
    ) -> int:
        count = 0
        for w in self._workflows.values():
            if tenant_id and w.get("tenant_id", "tenant-default") != tenant_id:
                continue
            if state and w.get("state") != state:
                continue
            count += 1
        return count

    async def get_incomplete_workflows(self) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(wf) for wf in self._workflows.values()
            if wf.get("state") not in ("COMPLETED", "ESCALATED")
        ]

    async def save_step(self, workflow_id: str, step_data: dict[str, Any]) -> None:
        if workflow_id not in self._steps:
            self._steps[workflow_id] = {}
        self._steps[workflow_id][step_data["step_id"]] = copy.deepcopy(step_data)

    async def get_step(self, workflow_id: str, step_id: str) -> dict[str, Any] | None:
        step = self._steps.get(workflow_id, {}).get(step_id)
        return copy.deepcopy(step) if step else None

    async def get_steps(self, workflow_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(s) for s in self._steps.get(workflow_id, {}).values()]

    async def append_event(self, workflow_id: str, event_data: dict[str, Any]) -> None:
        if workflow_id not in self._events:
            self._events[workflow_id] = []
        self._events[workflow_id].append(copy.deepcopy(event_data))

    async def get_events(self, workflow_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(e) for e in self._events.get(workflow_id, [])]

    async def save_evidence(self, workflow_id: str, evidence_data: dict[str, Any]) -> None:
        if workflow_id not in self._evidence:
            self._evidence[workflow_id] = {}
        self._evidence[workflow_id][evidence_data["evidence_id"]] = copy.deepcopy(evidence_data)

    async def get_evidence(self, workflow_id: str, evidence_id: str) -> dict[str, Any] | None:
        ev = self._evidence.get(workflow_id, {}).get(evidence_id)
        return copy.deepcopy(ev) if ev else None

    async def get_all_evidence(self, workflow_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(e) for e in self._evidence.get(workflow_id, {}).values()]

    async def save_failure(self, workflow_id: str, failure_data: dict[str, Any]) -> None:
        if workflow_id not in self._failures:
            self._failures[workflow_id] = {}
        self._failures[workflow_id][failure_data["failure_id"]] = copy.deepcopy(failure_data)

    async def get_failure(self, workflow_id: str, failure_id: str) -> dict[str, Any] | None:
        f = self._failures.get(workflow_id, {}).get(failure_id)
        return copy.deepcopy(f) if f else None

    async def get_failures(self, workflow_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(f) for f in self._failures.get(workflow_id, {}).values()]

    async def save_recovery_plan(self, workflow_id: str, plan_data: dict[str, Any]) -> None:
        if workflow_id not in self._recovery_plans:
            self._recovery_plans[workflow_id] = {}
        self._recovery_plans[workflow_id][plan_data["plan_id"]] = copy.deepcopy(plan_data)

    async def get_recovery_plan(self, workflow_id: str, plan_id: str) -> dict[str, Any] | None:
        p = self._recovery_plans.get(workflow_id, {}).get(plan_id)
        return copy.deepcopy(p) if p else None

    async def get_recovery_plans(self, workflow_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(p) for p in self._recovery_plans.get(workflow_id, {}).values()]

    async def get_active_recovery_plan(
        self, workflow_id: str, target_outcome_id: str | None = None
    ) -> dict[str, Any] | None:
        plans = list(self._recovery_plans.get(workflow_id, {}).values())
        for p in reversed(plans):
            if target_outcome_id and p.get("target_outcome_id") != target_outcome_id:
                continue
            if p.get("status") in ("PROPOSED", "APPROVED", "EXECUTING"):
                return copy.deepcopy(p)
        return None

    async def save_approval(self, workflow_id: str, approval_data: dict[str, Any]) -> None:
        if workflow_id not in self._approvals:
            self._approvals[workflow_id] = {}
        self._approvals[workflow_id][approval_data["approval_id"]] = copy.deepcopy(approval_data)

    async def get_approval(self, workflow_id: str, approval_id: str) -> dict[str, Any] | None:
        a = self._approvals.get(workflow_id, {}).get(approval_id)
        return copy.deepcopy(a) if a else None

    async def get_approvals(self, workflow_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(a) for a in self._approvals.get(workflow_id, {}).values()]

    async def get_pending_approvals(self, workflow_id: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(a) for a in self._approvals.get(workflow_id, {}).values()
            if a.get("status") == "PENDING"
        ]

    async def get_pending_approval_by_dedup(
        self, workflow_id: str, dedup_key: str
    ) -> dict[str, Any] | None:
        for a in self._approvals.get(workflow_id, {}).values():
            if a.get("dedup_key") == dedup_key and a.get("status") == "PENDING":
                return copy.deepcopy(a)
        return None

    async def get_approved_action(
        self, workflow_id: str, action_tool: str, action_args: dict[str, Any]
    ) -> dict[str, Any] | None:
        clean_args = {k: v for k, v in action_args.items() if k not in ("workflow_id", "step_id")}
        for a in self._approvals.get(workflow_id, {}).values():
            if a.get("status") == "APPROVED" and a.get("action_tool") == action_tool:
                stored_args = {
                    k: v for k, v in a.get("action_args", {}).items()
                    if k not in ("workflow_id", "step_id")
                }
                if clean_args == stored_args:
                    return copy.deepcopy(a)
        return None

    async def get_idempotency_record(self, key: str) -> dict[str, Any] | None:
        record = self._idempotency.get(key)
        if record:
            expires = record.get("expires_at", "")
            if expires and expires < datetime.now(timezone.utc).isoformat():
                del self._idempotency[key]
                return None
            return copy.deepcopy(record)
        return None

    async def save_idempotency_record(self, key: str, record_data: dict[str, Any]) -> None:
        self._idempotency[key] = copy.deepcopy(record_data)

    # ------------------------------------------------------------------
    # Distributed Operation Claims Implementation
    # ------------------------------------------------------------------

    async def claim_operation(
        self,
        idempotency_key: str,
        workflow_id: str,
        tool_name: str,
        target_entity_id: str,
        parameters: dict[str, Any],
        worker_id: str,
        lease_seconds: int = 60,
    ) -> tuple[bool, dict[str, Any]]:
        async with self._global_lock:
            now = datetime.now(timezone.utc)
            lease_expires = now + timedelta(seconds=lease_seconds)

            current = self._operations.get(idempotency_key)
            if current:
                # If already COMPLETED, cannot claim; return completed record
                if current.get("status") == OperationStatus.COMPLETED.value:
                    return False, copy.deepcopy(current)

                # Check lease expiration
                exp_str = current.get("lease_expires_at", "")
                is_expired = False
                if exp_str:
                    try:
                        exp_dt = datetime.fromisoformat(exp_str)
                        if now > exp_dt:
                            is_expired = True
                    except ValueError:
                        is_expired = True

                # If lease is active and owned by another worker, reject claim
                if not is_expired and current.get("owner_worker_id") != worker_id and current.get("status") in (
                    OperationStatus.CLAIMED.value, OperationStatus.EXECUTING.value
                ):
                    return False, copy.deepcopy(current)

                # Stale lease or re-acquired by same worker -> update claim
                current["status"] = OperationStatus.CLAIMED.value
                current["owner_worker_id"] = worker_id
                current["lease_expires_at"] = lease_expires.isoformat()
                current["updated_at"] = now.isoformat()
                current["version"] = current.get("version", 1) + 1
                self._operations[idempotency_key] = current
                return True, copy.deepcopy(current)

            # No prior claim -> create new claim
            new_claim = OperationClaim(
                idempotency_key=idempotency_key,
                workflow_id=workflow_id,
                tool_name=tool_name,
                target_entity_id=target_entity_id,
                parameters=parameters,
                status=OperationStatus.CLAIMED,
                owner_worker_id=worker_id,
                lease_expires_at=lease_expires,
                created_at=now,
                updated_at=now,
                version=1,
            ).model_dump(mode="json")
            self._operations[idempotency_key] = new_claim
            return True, copy.deepcopy(new_claim)

    async def complete_operation(
        self,
        idempotency_key: str,
        result: dict[str, Any],
        worker_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        async with self._global_lock:
            current = self._operations.get(idempotency_key)
            if not current:
                current = {
                    "idempotency_key": idempotency_key,
                    "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

            if expected_version is not None and current.get("version") != expected_version:
                raise StaleWorkflowStateError(
                    f"OCC Conflict on Operation '{idempotency_key}': version is {current.get('version')}, expected {expected_version}"
                )

            current["status"] = OperationStatus.COMPLETED.value
            current["result"] = copy.deepcopy(result)
            current["completed_at"] = datetime.now(timezone.utc).isoformat()
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            current["version"] = current.get("version", 1) + 1
            if worker_id:
                current["owner_worker_id"] = worker_id
            self._operations[idempotency_key] = current

            # Mirror to idempotency record for backward compatibility
            self._idempotency[idempotency_key] = {
                "idempotency_key": idempotency_key,
                "workflow_id": current.get("workflow_id", ""),
                "status": "SUCCEEDED",
                "result": copy.deepcopy(result),
                "updated_at": current["updated_at"],
            }
            return copy.deepcopy(current)

    async def fail_operation(
        self,
        idempotency_key: str,
        error: str,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._global_lock:
            current = self._operations.get(idempotency_key)
            if not current:
                current = {
                    "idempotency_key": idempotency_key,
                    "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            current["status"] = OperationStatus.FAILED.value
            current["error"] = error
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            current["version"] = current.get("version", 1) + 1
            if worker_id:
                current["owner_worker_id"] = worker_id
            self._operations[idempotency_key] = current

            # Mirror to idempotency record for backward compatibility
            self._idempotency[idempotency_key] = {
                "idempotency_key": idempotency_key,
                "workflow_id": current.get("workflow_id", ""),
                "status": "FAILED",
                "error": error,
                "updated_at": current["updated_at"],
            }
            return copy.deepcopy(current)

    async def get_operation(self, idempotency_key: str) -> dict[str, Any] | None:
        data = self._operations.get(idempotency_key)
        return copy.deepcopy(data) if data else None

    async def save_audit_event(self, audit_data: dict[str, Any]) -> None:
        async with self._global_lock:
            self._audit_events.append(copy.deepcopy(audit_data))

    async def list_audit_events(
        self,
        tenant_id: str | None = None,
        workflow_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._global_lock:
            results: list[dict[str, Any]] = []
            for ev in self._audit_events:
                if tenant_id and ev.get("tenant_id") != tenant_id and tenant_id != "all":
                    continue
                if workflow_id and ev.get("workflow_id") != workflow_id:
                    continue
                if event_type and ev.get("event_type") != event_type:
                    continue
                results.append(copy.deepcopy(ev))

            results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
            if offset > 0:
                results = results[offset:]
            if limit > 0:
                results = results[:limit]
            return results

    async def get_workflow_snapshot(self, workflow_id: str) -> dict[str, Any] | None:
        workflow = await self.get_workflow(workflow_id)
        if not workflow:
            return None
        return {
            "workflow": workflow,
            "steps": await self.get_steps(workflow_id),
            "events": await self.get_events(workflow_id),
            "evidence": await self.get_all_evidence(workflow_id),
            "failures": await self.get_failures(workflow_id),
            "recovery_plans": await self.get_recovery_plans(workflow_id),
            "approvals": await self.get_approvals(workflow_id),
        }


class FirestoreWorkflowStore(BaseWorkflowStore):
    """
    Google Cloud Firestore implementation of BaseWorkflowStore with atomic transactions.
    Connects to Firestore emulator or live GCP project.
    """

    def __init__(
        self,
        project_id: str | None = None,
        emulator_host: str | None = None,
        database: str | None = None,
        credentials: Any | None = None,
    ) -> None:
        from google.cloud import firestore

        self._project_id = project_id or config.google_cloud_project or "recoveryos-local"
        self._emulator_host = emulator_host or config.firestore_emulator_host
        self._database = database or config.firestore_database or "(default)"
        self._credentials = credentials
        self._client: firestore.AsyncClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    async def _get_client(self):
        if self._client is None:
            from google.cloud import firestore
            kwargs: dict[str, Any] = {"project": self._project_id}
            if self._database and self._database != "(default)":
                kwargs["database"] = self._database
            if self._credentials is not None:
                kwargs["credentials"] = self._credentials
            self._client = firestore.AsyncClient(**kwargs)
        return self._client

    def get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def save_workflow(
        self, workflow_data: dict[str, Any], expected_version: int | None = None
    ) -> None:
        client = await self._get_client()
        wf_id = workflow_data["workflow_id"]
        doc_ref = client.collection("workflows").document(wf_id)

        data = copy.deepcopy(workflow_data)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()

        if expected_version is not None:
            from google.cloud import firestore

            @firestore.async_transactional
            async def _update_with_occ(transaction):
                snapshot = await doc_ref.get(transaction=transaction)
                if snapshot.exists:
                    current_ver = snapshot.get("version") or 1
                    if current_ver != expected_version:
                        raise StaleWorkflowStateError(
                            f"OCC Conflict: Workflow '{wf_id}' is at version {current_ver}, expected {expected_version}"
                        )
                    data["version"] = current_ver + 1
                else:
                    data["version"] = 1
                transaction.set(doc_ref, data)

            transaction = client.transaction()
            await _update_with_occ(transaction)
        else:
            snapshot = await doc_ref.get()
            if snapshot.exists:
                data["version"] = (snapshot.get("version") or 1) + 1
            else:
                data["version"] = data.get("version", 1)
            await doc_ref.set(data)

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        client = await self._get_client()
        doc = await client.collection("workflows").document(workflow_id).get()
        return doc.to_dict() if doc.exists else None

    async def list_workflows(
        self,
        tenant_id: str | None = None,
        state: str | None = None,
        scenario: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        client = await self._get_client()
        query = client.collection("workflows")
        if tenant_id:
            query = query.where("tenant_id", "==", tenant_id)
        if state:
            query = query.where("state", "==", state)
        if scenario:
            query = query.where("scenario", "==", scenario)

        docs = query.stream()
        results = [doc.to_dict() async for doc in docs]
        results.sort(
            key=lambda x: x.get("updated_at") or x.get("created_at") or "",
            reverse=True,
        )
        if offset > 0:
            results = results[offset:]
        if limit is not None and limit > 0:
            results = results[:limit]
        return results

    async def count_workflows(
        self,
        tenant_id: str | None = None,
        state: str | None = None,
    ) -> int:
        client = await self._get_client()
        query = client.collection("workflows")
        if tenant_id:
            query = query.where("tenant_id", "==", tenant_id)
        if state:
            query = query.where("state", "==", state)
        docs = query.stream()
        count = 0
        async for _ in docs:
            count += 1
        return count

    async def get_incomplete_workflows(self) -> list[dict[str, Any]]:
        client = await self._get_client()
        docs = client.collection("workflows").stream()
        results = []
        async for doc in docs:
            d = doc.to_dict()
            if d.get("state") not in ("COMPLETED", "ESCALATED"):
                results.append(d)
        return results

    async def save_step(self, workflow_id: str, step_data: dict[str, Any]) -> None:
        client = await self._get_client()
        step_id = step_data["step_id"]
        doc_ref = client.collection("workflows").document(workflow_id).collection("steps").document(step_id)
        await doc_ref.set(step_data)

    async def get_step(self, workflow_id: str, step_id: str) -> dict[str, Any] | None:
        client = await self._get_client()
        doc = await client.collection("workflows").document(workflow_id).collection("steps").document(step_id).get()
        return doc.to_dict() if doc.exists else None

    async def get_steps(self, workflow_id: str) -> list[dict[str, Any]]:
        client = await self._get_client()
        docs = client.collection("workflows").document(workflow_id).collection("steps").stream()
        return [doc.to_dict() async for doc in docs]

    async def append_event(self, workflow_id: str, event_data: dict[str, Any]) -> None:
        client = await self._get_client()
        event_id = event_data.get("event_id") or f"ev-{datetime.now(timezone.utc).timestamp()}"
        doc_ref = client.collection("workflows").document(workflow_id).collection("events").document(event_id)
        await doc_ref.set(event_data)

    async def get_events(self, workflow_id: str) -> list[dict[str, Any]]:
        client = await self._get_client()
        docs = client.collection("workflows").document(workflow_id).collection("events").order_by("timestamp").stream()
        return [doc.to_dict() async for doc in docs]

    async def save_evidence(self, workflow_id: str, evidence_data: dict[str, Any]) -> None:
        client = await self._get_client()
        ev_id = evidence_data["evidence_id"]
        doc_ref = client.collection("workflows").document(workflow_id).collection("evidence").document(ev_id)
        await doc_ref.set(evidence_data)

    async def get_evidence(self, workflow_id: str, evidence_id: str) -> dict[str, Any] | None:
        client = await self._get_client()
        doc = await client.collection("workflows").document(workflow_id).collection("evidence").document(evidence_id).get()
        return doc.to_dict() if doc.exists else None

    async def get_all_evidence(self, workflow_id: str) -> list[dict[str, Any]]:
        client = await self._get_client()
        docs = client.collection("workflows").document(workflow_id).collection("evidence").stream()
        return [doc.to_dict() async for doc in docs]

    async def save_failure(self, workflow_id: str, failure_data: dict[str, Any]) -> None:
        client = await self._get_client()
        f_id = failure_data["failure_id"]
        doc_ref = client.collection("workflows").document(workflow_id).collection("failures").document(f_id)
        await doc_ref.set(failure_data)

    async def get_failure(self, workflow_id: str, failure_id: str) -> dict[str, Any] | None:
        client = await self._get_client()
        doc = await client.collection("workflows").document(workflow_id).collection("failures").document(failure_id).get()
        return doc.to_dict() if doc.exists else None

    async def get_failures(self, workflow_id: str) -> list[dict[str, Any]]:
        client = await self._get_client()
        docs = client.collection("workflows").document(workflow_id).collection("failures").stream()
        return [doc.to_dict() async for doc in docs]

    async def save_recovery_plan(self, workflow_id: str, plan_data: dict[str, Any]) -> None:
        client = await self._get_client()
        plan_id = plan_data["plan_id"]
        doc_ref = client.collection("workflows").document(workflow_id).collection("recovery_plans").document(plan_id)
        await doc_ref.set(plan_data)

    async def get_recovery_plan(self, workflow_id: str, plan_id: str) -> dict[str, Any] | None:
        client = await self._get_client()
        doc = await client.collection("workflows").document(workflow_id).collection("recovery_plans").document(plan_id).get()
        return doc.to_dict() if doc.exists else None

    async def get_recovery_plans(self, workflow_id: str) -> list[dict[str, Any]]:
        client = await self._get_client()
        docs = client.collection("workflows").document(workflow_id).collection("recovery_plans").stream()
        return [doc.to_dict() async for doc in docs]

    async def get_active_recovery_plan(
        self, workflow_id: str, target_outcome_id: str | None = None
    ) -> dict[str, Any] | None:
        plans = await self.get_recovery_plans(workflow_id)
        for p in reversed(plans):
            if target_outcome_id and p.get("target_outcome_id") != target_outcome_id:
                continue
            if p.get("status") in ("PROPOSED", "APPROVED", "EXECUTING"):
                return p
        return None

    async def save_approval(self, workflow_id: str, approval_data: dict[str, Any]) -> None:
        client = await self._get_client()
        appr_id = approval_data["approval_id"]
        doc_ref = client.collection("workflows").document(workflow_id).collection("approvals").document(appr_id)
        await doc_ref.set(approval_data)

    async def get_approval(self, workflow_id: str, approval_id: str) -> dict[str, Any] | None:
        client = await self._get_client()
        doc = await client.collection("workflows").document(workflow_id).collection("approvals").document(approval_id).get()
        return doc.to_dict() if doc.exists else None

    async def get_approvals(self, workflow_id: str) -> list[dict[str, Any]]:
        client = await self._get_client()
        docs = client.collection("workflows").document(workflow_id).collection("approvals").stream()
        return [doc.to_dict() async for doc in docs]

    async def get_pending_approvals(self, workflow_id: str) -> list[dict[str, Any]]:
        approvals = await self.get_approvals(workflow_id)
        return [a for a in approvals if a.get("status") == "PENDING"]

    async def get_pending_approval_by_dedup(
        self, workflow_id: str, dedup_key: str
    ) -> dict[str, Any] | None:
        approvals = await self.get_approvals(workflow_id)
        for a in approvals:
            if a.get("dedup_key") == dedup_key and a.get("status") == "PENDING":
                return a
        return None

    async def get_approved_action(
        self, workflow_id: str, action_tool: str, action_args: dict[str, Any]
    ) -> dict[str, Any] | None:
        clean_args = {k: v for k, v in action_args.items() if k not in ("workflow_id", "step_id")}
        approvals = await self.get_approvals(workflow_id)
        for a in approvals:
            if a.get("status") == "APPROVED" and a.get("action_tool") == action_tool:
                stored_args = {
                    k: v for k, v in a.get("action_args", {}).items()
                    if k not in ("workflow_id", "step_id")
                }
                if clean_args == stored_args:
                    return a
        return None

    async def get_idempotency_record(self, key: str) -> dict[str, Any] | None:
        client = await self._get_client()
        doc = await client.collection("idempotency_records").document(key).get()
        if doc.exists:
            record = doc.to_dict()
            expires = record.get("expires_at", "")
            if expires and expires < datetime.now(timezone.utc).isoformat():
                await client.collection("idempotency_records").document(key).delete()
                return None
            return record
        return None

    async def save_idempotency_record(self, key: str, record_data: dict[str, Any]) -> None:
        client = await self._get_client()
        doc_ref = client.collection("idempotency_records").document(key)
        await doc_ref.set(record_data)

    # ------------------------------------------------------------------
    # Distributed Operation Claims (Firestore Transactions)
    # ------------------------------------------------------------------

    async def claim_operation(
        self,
        idempotency_key: str,
        workflow_id: str,
        tool_name: str,
        target_entity_id: str,
        parameters: dict[str, Any],
        worker_id: str,
        lease_seconds: int = 60,
    ) -> tuple[bool, dict[str, Any]]:
        client = await self._get_client()
        doc_ref = client.collection("operation_claims").document(idempotency_key)
        from google.cloud import firestore

        now = datetime.now(timezone.utc)
        lease_expires = now + timedelta(seconds=lease_seconds)

        @firestore.async_transactional
        async def _claim_tx(transaction):
            doc = await doc_ref.get(transaction=transaction)
            if doc.exists:
                current = doc.to_dict()
                if current.get("status") == OperationStatus.COMPLETED.value:
                    return False, current

                exp_str = current.get("lease_expires_at", "")
                is_expired = False
                if exp_str:
                    try:
                        exp_dt = datetime.fromisoformat(exp_str)
                        if now > exp_dt:
                            is_expired = True
                    except ValueError:
                        is_expired = True

                if not is_expired and current.get("owner_worker_id") != worker_id and current.get("status") in (
                    OperationStatus.CLAIMED.value, OperationStatus.EXECUTING.value
                ):
                    return False, current

                current["status"] = OperationStatus.CLAIMED.value
                current["owner_worker_id"] = worker_id
                current["lease_expires_at"] = lease_expires.isoformat()
                current["updated_at"] = now.isoformat()
                current["version"] = current.get("version", 1) + 1
                transaction.set(doc_ref, current)
                return True, current
            else:
                new_claim = OperationClaim(
                    idempotency_key=idempotency_key,
                    workflow_id=workflow_id,
                    tool_name=tool_name,
                    target_entity_id=target_entity_id,
                    parameters=parameters,
                    status=OperationStatus.CLAIMED,
                    owner_worker_id=worker_id,
                    lease_expires_at=lease_expires,
                    created_at=now,
                    updated_at=now,
                    version=1,
                ).model_dump(mode="json")
                transaction.set(doc_ref, new_claim)
                return True, new_claim

        tx = client.transaction()
        return await _claim_tx(tx)

    async def complete_operation(
        self,
        idempotency_key: str,
        result: dict[str, Any],
        worker_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        doc_ref = client.collection("operation_claims").document(idempotency_key)
        from google.cloud import firestore

        @firestore.async_transactional
        async def _complete_tx(transaction):
            doc = await doc_ref.get(transaction=transaction)
            current = doc.to_dict() if doc.exists else {"idempotency_key": idempotency_key, "version": 1}
            if expected_version is not None and current.get("version") != expected_version:
                raise StaleWorkflowStateError(
                    f"OCC Conflict on Operation '{idempotency_key}': version is {current.get('version')}, expected {expected_version}"
                )
            current["status"] = OperationStatus.COMPLETED.value
            current["result"] = result
            current["completed_at"] = datetime.now(timezone.utc).isoformat()
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            current["version"] = current.get("version", 1) + 1
            if worker_id:
                current["owner_worker_id"] = worker_id
            transaction.set(doc_ref, current)
            return current

        tx = client.transaction()
        res = await _complete_tx(tx)
        # Mirror to idempotency
        await self.save_idempotency_record(idempotency_key, {
            "idempotency_key": idempotency_key,
            "status": "SUCCEEDED",
            "result": result,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return res

    async def fail_operation(
        self,
        idempotency_key: str,
        error: str,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        doc_ref = client.collection("operation_claims").document(idempotency_key)
        now_str = datetime.now(timezone.utc).isoformat()
        doc = await doc_ref.get()
        current = doc.to_dict() if doc.exists else {"idempotency_key": idempotency_key, "version": 1}
        current["status"] = OperationStatus.FAILED.value
        current["error"] = error
        current["updated_at"] = now_str
        current["version"] = current.get("version", 1) + 1
        if worker_id:
            current["owner_worker_id"] = worker_id
        await doc_ref.set(current)
        await self.save_idempotency_record(idempotency_key, {
            "idempotency_key": idempotency_key,
            "status": "FAILED",
            "error": error,
            "updated_at": now_str,
        })
        return current

    async def get_operation(self, idempotency_key: str) -> dict[str, Any] | None:
        client = await self._get_client()
        doc = await client.collection("operation_claims").document(idempotency_key).get()
        return doc.to_dict() if doc.exists else None

    async def save_audit_event(self, audit_data: dict[str, Any]) -> None:
        client = await self._get_client()
        audit_id = audit_data.get("audit_id") or f"audit-{datetime.now(timezone.utc).timestamp()}"
        doc_ref = client.collection("audit_events").document(audit_id)
        await doc_ref.set(audit_data)

    async def list_audit_events(
        self,
        tenant_id: str | None = None,
        workflow_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        client = await self._get_client()
        query = client.collection("audit_events")
        if tenant_id and tenant_id != "all":
            query = query.where("tenant_id", "==", tenant_id)
        if workflow_id:
            query = query.where("workflow_id", "==", workflow_id)
        if event_type:
            query = query.where("event_type", "==", event_type)

        docs = query.stream()
        results = [doc.to_dict() async for doc in docs]
        results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        if offset > 0:
            results = results[offset:]
        if limit > 0:
            results = results[:limit]
        return results

    async def get_workflow_snapshot(self, workflow_id: str) -> dict[str, Any] | None:
        workflow = await self.get_workflow(workflow_id)
        if not workflow:
            return None
        return {
            "workflow": workflow,
            "steps": await self.get_steps(workflow_id),
            "events": await self.get_events(workflow_id),
            "evidence": await self.get_all_evidence(workflow_id),
            "failures": await self.get_failures(workflow_id),
            "recovery_plans": await self.get_recovery_plans(workflow_id),
            "approvals": await self.get_approvals(workflow_id),
        }


def create_workflow_store(backend: str | None = None) -> BaseWorkflowStore:
    """Factory creating the appropriate BaseWorkflowStore instance."""
    selected = backend or config.persistence_backend
    if selected == "firestore" or (selected == "in_memory" and config.use_firestore_emulator and backend == "firestore"):
        return FirestoreWorkflowStore(
            project_id=config.google_cloud_project,
            database=config.firestore_database,
        )
    return InMemoryWorkflowStore()


# Default alias for backward compatibility across existing codebase
WorkflowStore = InMemoryWorkflowStore
