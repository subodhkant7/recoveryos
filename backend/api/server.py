"""
RecoveryOS FastAPI Server.

API routes for workflow management, agent execution, human approval,
and real-time event streaming.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.config import config
from backend.agents.agent_factory import AgentFactory
from backend.engine.policy_engine import PolicyEngine
from backend.engine.workflow_engine import WorkflowEngine
from backend.persistence.workflow_store import create_workflow_store, BaseWorkflowStore
from backend.simulation.external_services import SimulatedServices
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.scenarios import (
    ACME_CUSTOMER_DATA,
    create_acme_contract,
    configure_demo_scenario,
)
from backend.models.workflow import WorkflowState
from backend.models.events import EventType
from backend.events import (
    BaseEventPublisher,
    EventPublishError,
    WorkflowEventType,
    WorkflowExecutionMessage,
    create_event_publisher,
)
from backend.security.principal import Principal, Role, Permission
from backend.security.dependencies import get_current_principal, require_role, require_permission
from backend.security.audit import record_security_audit_event
from backend.observability.logging import (
    current_request_id,
    EVENT_WORKFLOW_DISPATCHED,
    EVENT_WORKFLOW_PUBLISH_FAILED,
    EVENT_WORKFLOW_RECOVERED,
)
from backend.observability.metrics import (
    metrics,
    record_workflow_dispatched,
    record_publish_failure,
    record_workflow_recovery,
)
from backend.observability.middleware import CorrelationAndMetricsMiddleware
from backend.lifecycle import lifespan, shutdown_manager

logger = logging.getLogger("recoveryos.api.server")

# ---------------------------------------------------------------------------
# Application Setup
# ---------------------------------------------------------------------------

# Validate production configuration fail-closed rules
config.validate_production_config()

app = FastAPI(
    title="RecoveryOS",
    description="Autonomous operations system — agentic failure recovery",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(CorrelationAndMetricsMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared state (singleton instances)
failure_injector = FailureInjector()
services = SimulatedServices(failure_injector)
store: BaseWorkflowStore = create_workflow_store(config.persistence_backend)
engine = WorkflowEngine(store)
policy_engine = PolicyEngine()
agent_factory = AgentFactory(store, engine, services, policy_engine)

_event_publisher: BaseEventPublisher | None = None


def get_event_publisher() -> BaseEventPublisher:
    global _event_publisher
    if _event_publisher is None:
        _event_publisher = create_event_publisher(config.event_publisher_backend)
    return _event_publisher


def set_event_publisher(publisher: BaseEventPublisher | None) -> None:
    global _event_publisher
    _event_publisher = publisher


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class LaunchScenarioRequest(BaseModel):
    scenario: str = "billing_unavailable"


class ApprovalRequest(BaseModel):
    approved: bool
    reason: str = ""
    decided_by: str | None = None  # Ignored by server; server stamps authenticated principal


class WorkflowRecoveryRequest(BaseModel):
    reason: str = "Operator requested workflow recovery"
    force: bool = False  # If true, allows recovering ESCALATED workflows


# ---------------------------------------------------------------------------
# Tenant & Workflow Authorization Helpers
# ---------------------------------------------------------------------------


async def _get_authorized_workflow(
    workflow_id: str, principal: Principal | None = None
) -> dict[str, Any]:
    """Retrieve workflow ensuring cross-tenant isolation rules."""
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    if principal is not None and isinstance(principal, Principal):
        wf_tenant = wf.get("tenant_id", "tenant-default")
        if not principal.can_access_tenant(wf_tenant):
            record_security_audit_event(
                event_type="AUTH_DENIAL",
                actor_id=principal.user_id,
                role=principal.role.value,
                tenant_id=principal.tenant_id,
                workflow_id=workflow_id,
                action="cross_tenant_read",
                outcome="DENIED",
                reason=f"Principal tenant '{principal.tenant_id}' cannot access workflow tenant '{wf_tenant}'",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-tenant access forbidden",
            )
    return wf


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------


@app.get("/metrics")
async def get_metrics():
    """PUBLIC / MONITORING endpoint: Export Prometheus text metrics."""
    return Response(
        content=metrics.generate_prometheus_text(),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/api/health")
async def health():
    """PUBLIC endpoint: Liveness probe checking process health."""
    return {
        "status": "healthy",
        "service": "recoveryos",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": config.gemini_model,
        "environment": config.environment,
    }


@app.get("/api/ready")
async def ready():
    """PUBLIC endpoint: Readiness probe verifying backend dependency health."""
    readiness_data = {
        "status": "ready",
        "service": "recoveryos",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "persistence_backend": config.persistence_backend,
    }

    if config.persistence_backend == "firestore":
        if not config.firestore_emulator_host and not config.google_cloud_project:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Firestore persistence unconfigured",
            )
        # Check client accessibility if available
        if hasattr(store, "_db") and store._db is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Firestore client not initialized",
            )

    return readiness_data


@app.post("/api/scenarios/{scenario_name}")
async def launch_scenario(
    scenario_name: str,
    principal: Principal = Depends(require_role(Role.OPERATOR, Role.ADMIN)),
):
    """
    OPERATOR / ADMIN endpoint: Launch a demo scenario.
    """
    if shutdown_manager.is_shutting_down:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server is shutting down; new workflows rejected",
        )

    workflow_id = str(uuid.uuid4())
    contract = create_acme_contract(workflow_id)

    wf_data = await engine.create_workflow(
        name=f"ACME Corp Onboarding — {scenario_name}",
        scenario=scenario_name,
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        tenant_id=principal.tenant_id,
    )
    workflow_id = wf_data["workflow_id"]

    metrics.inc_counter(
        "recoveryos_workflow_creations_total",
        labels={"scenario": scenario_name, "status": "launched"},
    )

    # Configure failure injection
    configure_demo_scenario(failure_injector, workflow_id, scenario_name)

    record_security_audit_event(
        event_type="PRIVILEGED_MUTATION",
        actor_id=principal.user_id,
        role=principal.role.value,
        tenant_id=principal.tenant_id,
        workflow_id=workflow_id,
        action="launch_scenario",
        outcome="ALLOWED",
        reason=f"Launched scenario {scenario_name}",
    )

    if config.event_publisher_backend == "pubsub":
        publisher = get_event_publisher()
        corr_id = current_request_id.get() or str(uuid.uuid4())
        workflow_ver = wf_data.get("version", 1)
        idemp_key = f"op_dispatch_{workflow_id}_v{workflow_ver}"

        msg = WorkflowExecutionMessage(
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            workflow_id=workflow_id,
            tenant_id=principal.tenant_id,
            idempotency_key=idemp_key,
            expected_version=workflow_ver,
            correlation_id=corr_id,
            producer_id="recoveryos-api",
            payload={"scenario": scenario_name},
        )

        try:
            pubsub_msg_id = await publisher.publish_workflow_execution(msg)
            record_workflow_dispatched(scenario=scenario_name, tenant_id=principal.tenant_id)
        except Exception as e:
            record_publish_failure(backend="pubsub")
            logger.error(
                "Failed to dispatch workflow execution to Pub/Sub",
                extra={
                    "event_name": EVENT_WORKFLOW_PUBLISH_FAILED,
                    "workflow_id": workflow_id,
                    "tenant_id": principal.tenant_id,
                    "error": str(e),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to dispatch workflow execution to messaging backend: {e}",
            )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "dispatched",
                "workflow_id": workflow_id,
                "scenario": scenario_name,
                "tenant_id": principal.tenant_id,
                "message": "Workflow dispatched asynchronously for background execution.",
                "pubsub_message_id": pubsub_msg_id,
            },
        )
    else:
        # Start agent execution in background with shutdown manager tracking for local in-process mode
        record_workflow_dispatched(scenario=scenario_name, tenant_id=principal.tenant_id)
        task = asyncio.create_task(_run_agent(workflow_id))
        shutdown_manager.register_task(task)

        return {
            "status": "launched",
            "workflow_id": workflow_id,
            "scenario": scenario_name,
            "tenant_id": principal.tenant_id,
            "message": "Workflow started. Agent executing autonomously in background.",
        }


@app.get("/api/workflows")
async def list_workflows(
    principal: Principal = Depends(get_current_principal),
):
    """AUTHENTICATED endpoint: List workflows scoped to principal tenant."""
    all_workflows = await store.list_workflows()
    if principal.role == Role.ADMIN:
        return {"workflows": all_workflows}
    filtered = [w for w in all_workflows if w.get("tenant_id", "tenant-default") == principal.tenant_id]
    return {"workflows": filtered}


@app.get("/api/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """AUTHENTICATED endpoint: Get full workflow state including timeline."""
    await _get_authorized_workflow(workflow_id, principal)
    snapshot = await store.get_workflow_snapshot(workflow_id)
    if not snapshot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return snapshot


@app.get("/api/workflows/{workflow_id}/events")
async def get_events(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """AUTHENTICATED endpoint: Get workflow event timeline."""
    await _get_authorized_workflow(workflow_id, principal)
    events = await store.get_events(workflow_id)
    return {"events": events}


@app.get("/api/workflows/{workflow_id}/events/stream")
async def stream_events(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    AUTHENTICATED endpoint: SSE event stream for real-time workflow updates.
    """
    await _get_authorized_workflow(workflow_id, principal)

    async def event_generator():
        last_count = 0
        while True:
            events = await store.get_events(workflow_id)
            if len(events) > last_count:
                for event in events[last_count:]:
                    yield f"data: {json.dumps(event)}\n\n"
                last_count = len(events)

            # Check if workflow is terminal
            wf = await store.get_workflow(workflow_id)
            if wf and wf.get("state") in ("COMPLETED", "ESCALATED"):
                yield f"data: {json.dumps({'event_type': 'STREAM_END', 'state': wf['state']})}\n\n"
                break

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/workflows/{workflow_id}/approvals")
async def get_pending_approvals(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """AUTHENTICATED endpoint: Get pending human approval requests."""
    await _get_authorized_workflow(workflow_id, principal)
    approvals = await store.get_pending_approvals(workflow_id)
    return {"approvals": approvals}


@app.post("/api/workflows/{workflow_id}/approve/{approval_id}")
async def approve_workflow(
    workflow_id: str,
    approval_id: str,
    request: ApprovalRequest,
    principal: Principal = Depends(require_role(Role.APPROVER, Role.ADMIN)),
):
    """
    APPROVER / ADMIN endpoint: Submit human approval decision.
    Authoritatively stamps the authenticated principal as the decider.
    """
    if isinstance(principal, Principal):
        actual_principal = principal
    else:
        actual_principal = Principal(
            user_id=request.decided_by or "human_operator",
            role=Role.ADMIN,
            tenant_id="tenant-default",
        )
    wf = await _get_authorized_workflow(workflow_id, actual_principal)

    # 1. Guard against terminal workflow mutation
    if wf.get("state") in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value):
        record_security_audit_event(
            event_type="APPROVAL_ATTEMPT",
            actor_id=actual_principal.user_id,
            role=actual_principal.role.value,
            tenant_id=actual_principal.tenant_id,
            workflow_id=workflow_id,
            action="approve_workflow",
            outcome="DENIED",
            reason=f"Cannot decide approval on terminal workflow state '{wf.get('state')}'",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot decide approval on terminal workflow state '{wf.get('state')}'",
        )

    # 2. Check approval record existence and binding
    approval = await store.get_approval(workflow_id, approval_id)
    if not approval:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    if approval.get("status") != "PENDING":
        record_security_audit_event(
            event_type="APPROVAL_ATTEMPT",
            actor_id=actual_principal.user_id,
            role=actual_principal.role.value,
            tenant_id=actual_principal.tenant_id,
            workflow_id=workflow_id,
            action="approve_workflow",
            outcome="DENIED",
            reason=f"Approval already in status '{approval.get('status')}'",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Approval already decided",
        )

    # 3. Update approval authoritatively using verified principal identity
    decision_status = "APPROVED" if request.approved else "REJECTED"
    approval["status"] = decision_status
    approval["decided_at"] = datetime.now(timezone.utc).isoformat()
    approval["decided_by"] = actual_principal.user_id  # Authoritative identity
    approval["decision_reason"] = request.reason
    await store.save_approval(workflow_id, approval)

    # 4. Record security audit event
    record_security_audit_event(
        event_type="APPROVAL_SUCCESS" if request.approved else "APPROVAL_REJECTION",
        actor_id=actual_principal.user_id,
        role=actual_principal.role.value,
        tenant_id=actual_principal.tenant_id,
        workflow_id=workflow_id,
        action="approve_workflow",
        outcome="ALLOWED",
        reason=request.reason or ("Approved" if request.approved else "Rejected"),
        extra={"approval_id": approval_id, "approved": request.approved},
    )

    # 5. Record timeline event
    await engine._record_event(
        workflow_id=workflow_id,
        event_type=EventType.APPROVAL_DECIDED,
        title=f"Human {decision_status}",
        detail=request.reason or "No reason provided",
        payload={"approval_id": approval_id, "approved": request.approved, "decided_by": actual_principal.user_id},
        actor="human",
    )

    # 6. Transition workflow / dispatch execution
    if request.approved:
        if config.event_publisher_backend == "pubsub":
            publisher = get_event_publisher()
            corr_id = current_request_id.get() or str(uuid.uuid4())
            latest_wf = await store.get_workflow(workflow_id)
            current_ver = latest_wf.get("version", 1) if latest_wf else 1
            idemp_key = f"op_approve_{workflow_id}_{approval_id}_v{current_ver}"

            msg = WorkflowExecutionMessage(
                event_type=WorkflowEventType.APPROVAL_RESUME,
                workflow_id=workflow_id,
                tenant_id=actual_principal.tenant_id,
                idempotency_key=idemp_key,
                expected_version=current_ver,
                correlation_id=corr_id,
                producer_id="recoveryos-api",
                payload={"approval_id": approval_id, "approved": True},
            )

            try:
                pubsub_msg_id = await publisher.publish_workflow_execution(msg)
            except Exception as e:
                logger.error(
                    "Failed to dispatch approval resume to Pub/Sub",
                    extra={
                        "event_name": "API_APPROVAL_DISPATCH_FAILED",
                        "workflow_id": workflow_id,
                        "approval_id": approval_id,
                        "error": str(e),
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Failed to dispatch approval execution to messaging backend: {e}",
                )

            return {
                "status": "decided",
                "approved": True,
                "decided_by": actual_principal.user_id,
                "dispatched": True,
                "pubsub_message_id": pubsub_msg_id,
            }
        else:
            await engine.transition(
                workflow_id,
                WorkflowState.EXECUTING,
                detail="Human approved recovery plan",
                actor="human",
            )
            task = asyncio.create_task(_run_agent(workflow_id))
            shutdown_manager.register_task(task)
            return {"status": "decided", "approved": True, "decided_by": actual_principal.user_id}
    else:
        await engine.transition(
            workflow_id,
            WorkflowState.ESCALATED,
            detail=f"Human rejected: {request.reason}",
            actor="human",
        )
        return {"status": "decided", "approved": False, "decided_by": actual_principal.user_id}


# ---------------------------------------------------------------------------
# Phase 6.5 Operability & Recovery Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/workflows/{workflow_id}/diagnostics")
async def get_workflow_diagnostics(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    AUTHENTICATED endpoint: Analyze operational health, lease status, and stuck conditions.
    """
    wf = await _get_authorized_workflow(workflow_id, principal)
    snapshot = await store.get_workflow_snapshot(workflow_id)
    if not snapshot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    events = snapshot.get("events", [])
    now = datetime.now(timezone.utc)

    # Calculate age
    created_at_str = wf.get("created_at") or wf.get("dispatched_at")
    age_seconds = 0.0
    if created_at_str:
        try:
            created_dt = datetime.fromisoformat(created_at_str)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (now - created_dt).total_seconds())
        except Exception:
            pass

    # Check state & stuck conditions
    current_state = wf.get("state", "UNKNOWN")
    is_terminal = current_state in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value)

    # Check claim status in store if accessible
    claim_info = None
    if hasattr(store, "_operations") and isinstance(store._operations, dict):
        for k, v in store._operations.items():
            if isinstance(v, dict) and v.get("workflow_id") == workflow_id:
                claim_info = {
                    "idempotency_key": k,
                    "status": v.get("status"),
                    "owner_worker_id": v.get("owner_worker_id"),
                    "lease_expires_at": v.get("lease_expires_at"),
                }
                break

    is_stuck = False
    stuck_reason = None

    if not is_terminal:
        if current_state == WorkflowState.CREATED.value and age_seconds > 60.0:
            is_stuck = True
            stuck_reason = f"Workflow remained in CREATED state for {int(age_seconds)}s without worker pickup."
        elif current_state == WorkflowState.EXECUTING.value and age_seconds > 180.0:
            is_stuck = True
            stuck_reason = f"Workflow executing for {int(age_seconds)}s exceeding expected duration."
        elif claim_info and claim_info.get("status") == "CLAIMED":
            lease_exp_str = claim_info.get("lease_expires_at")
            if lease_exp_str:
                try:
                    exp_dt = datetime.fromisoformat(lease_exp_str)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if now > exp_dt:
                        is_stuck = True
                        stuck_reason = "Active operation claim lease has expired without task completion."
                except Exception:
                    pass

    return {
        "workflow_id": workflow_id,
        "tenant_id": wf.get("tenant_id", "tenant-default"),
        "state": current_state,
        "version": wf.get("version", 1),
        "age_seconds": round(age_seconds, 2),
        "is_terminal": is_terminal,
        "is_stuck": is_stuck,
        "stuck_reason": stuck_reason,
        "is_recoverable": (not is_terminal) or (current_state == WorkflowState.ESCALATED.value),
        "operation_claim": claim_info,
        "event_count": len(events),
        "last_event": events[-1] if events else None,
    }


@app.post("/api/workflows/{workflow_id}/recover")
async def recover_workflow(
    workflow_id: str,
    request: WorkflowRecoveryRequest = WorkflowRecoveryRequest(),
    principal: Principal = Depends(require_role(Role.OPERATOR, Role.ADMIN)),
):
    """
    OPERATOR / ADMIN endpoint: Safely and idempotently recover/redrive a stalled workflow.
    Validates tenant isolation, verifies non-terminal state, binds to current OCC version,
    and publishes a recovery dispatch event.
    """
    wf = await _get_authorized_workflow(workflow_id, principal)
    current_state = wf.get("state")

    if current_state == WorkflowState.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot recover a COMPLETED workflow; terminal states are immutable.",
        )

    if current_state == WorkflowState.ESCALATED.value and not request.force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow is in ESCALATED state. Resolve incident or pass force=true to redrive.",
        )

    current_version = wf.get("version", 1)
    corr_id = current_request_id.get() or f"corr-rec-{uuid.uuid4()}"
    recovery_idemp_key = f"op_recover_{workflow_id}_v{current_version}_{str(uuid.uuid4())[:8]}"

    record_security_audit_event(
        event_type="PRIVILEGED_MUTATION",
        actor_id=principal.user_id,
        role=principal.role.value,
        tenant_id=principal.tenant_id,
        workflow_id=workflow_id,
        action="recover_workflow",
        outcome="ALLOWED",
        reason=request.reason,
    )

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.RECOVERY_TRIGGER,
        workflow_id=workflow_id,
        tenant_id=wf.get("tenant_id", principal.tenant_id),
        idempotency_key=recovery_idemp_key,
        expected_version=current_version,
        correlation_id=corr_id,
        producer_id=f"recoveryos-operator-{principal.user_id}",
        payload={"reason": request.reason, "recovered_by": principal.user_id, "forced": request.force},
    )

    # Append recovery audit event
    await store.append_event(
        workflow_id=workflow_id,
        event_data={
            "event_type": EVENT_WORKFLOW_RECOVERED,
            "recovered_by": principal.user_id,
            "role": principal.role.value,
            "reason": request.reason,
            "version": current_version,
            "idempotency_key": recovery_idemp_key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    if config.event_publisher_backend == "pubsub":
        publisher = get_event_publisher()
        try:
            pubsub_msg_id = await publisher.publish_workflow_execution(msg)
            record_workflow_recovery(status="dispatched")
        except Exception as e:
            record_publish_failure("pubsub")
            logger.error(
                "Failed to publish workflow recovery event",
                extra={
                    "event_name": EVENT_WORKFLOW_PUBLISH_FAILED,
                    "workflow_id": workflow_id,
                    "tenant_id": principal.tenant_id,
                    "error": str(e),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to publish workflow recovery event: {e}",
            )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "recovery_dispatched",
                "workflow_id": workflow_id,
                "tenant_id": principal.tenant_id,
                "current_version": current_version,
                "idempotency_key": recovery_idemp_key,
                "pubsub_message_id": pubsub_msg_id,
                "message": "Workflow recovery dispatched successfully for background processing.",
            },
        )
    else:
        # In-process local execution mode
        task = asyncio.create_task(_run_agent(workflow_id))
        shutdown_manager.register_task(task)
        record_workflow_recovery(status="launched_local")
        return {
            "status": "recovery_launched",
            "workflow_id": workflow_id,
            "tenant_id": principal.tenant_id,
            "current_version": current_version,
            "message": "Workflow recovery agent started locally.",
        }


# ---------------------------------------------------------------------------
# Agent Execution (background task)
# ---------------------------------------------------------------------------


async def _run_agent(workflow_id: str) -> None:
    """
    Run the ADK agent for a workflow.

    This is the core execution loop. It creates an ADK session,
    sends the workflow context to the agent, and lets the agent
    call tools until the workflow reaches a terminal or paused state.
    """
    try:
        wf_data = await store.get_workflow(workflow_id)
        if not wf_data:
            return

        # Transition to EXECUTING if not already
        if wf_data["state"] == "CREATED":
            await engine.transition(
                workflow_id, WorkflowState.EXECUTING,
                detail="Agent beginning autonomous execution",
                actor="taskmaster",
            )

        # Build the initial prompt with workflow context
        snapshot = await store.get_workflow_snapshot(workflow_id)
        customer = wf_data.get("customer_data", {})
        contract = wf_data.get("contract", {})

        prompt = _build_agent_prompt(snapshot, customer, contract)

        # Create and run the agent
        orchestrator = agent_factory.create_orchestrator()

        # Use ADK's Runner to execute
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

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

        from google.genai import types

        # Send the prompt and let the agent work
        async for event in runner.run_async(
            session_id=session.id,
            user_id="system",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=prompt)],
            ),
        ):
            # Record agent reasoning events
            if hasattr(event, 'content') and event.content:
                for part in event.content.parts:
                    if hasattr(part, 'text') and part.text:
                        await engine._record_event(
                            workflow_id=workflow_id,
                            event_type=EventType.AGENT_REASONING,
                            title="Agent Response",
                            detail=part.text[:500],
                            payload={"full_text": part.text},
                            actor=event.author if hasattr(event, 'author') else "agent",
                        )

        # After agent finishes, check if all outcomes are verified
        wf_data = await store.get_workflow(workflow_id)
        if wf_data and wf_data["state"] == "EXECUTING":
            # Agent finished tool calls — transition to verifying
            await engine.transition(
                workflow_id, WorkflowState.VERIFYING,
                detail="Agent completed execution, verifying outcomes",
                actor="system",
            )

            # Check contract fulfillment
            contract = wf_data.get("contract", {})
            all_verified = all(
                o.get("verified", False)
                for o in contract.get("required_outcomes", [])
            )

            if all_verified:
                await engine.transition(
                    workflow_id, WorkflowState.COMPLETED,
                    detail="All outcomes independently verified",
                    actor="system",
                )
            else:
                # Some outcomes not verified — enter recovery
                unverified = [
                    o["outcome_id"]
                    for o in contract.get("required_outcomes", [])
                    if not o.get("verified", False)
                ]
                await engine.transition(
                    workflow_id, WorkflowState.RECOVERING,
                    detail=f"Unverified outcomes: {unverified}",
                    actor="system",
                )

    except Exception as e:
        # Record the error and transition workflow to a deterministic recoverable state (UNKNOWN)
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


def _build_agent_prompt(
    snapshot: dict[str, Any],
    customer: dict[str, Any],
    contract: dict[str, Any],
) -> str:
    """Build the initial prompt for the agent with full workflow context."""
    workflow_id = snapshot["workflow"]["workflow_id"]

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
        if o.get("acceptance_criteria"):
            prompt += f"  Criteria: {json.dumps(o['acceptance_criteria'])}\n"

    if constraints:
        prompt += "\nCONSTRAINTS:\n"
        for c in constraints:
            prompt += f"- {c['constraint_id']}: {c['description']}\n"

    if prohibited:
        prompt += f"\nPROHIBITED OUTCOMES: {', '.join(prohibited)}\n"

    if completed_steps:
        prompt += f"\nALREADY COMPLETED ({len(completed_steps)} steps):\n"
        for s in completed_steps:
            prompt += f"- {s.get('name', s.get('tool_name', 'unknown'))}: {s.get('result', {}).get('status', 'unknown')}\n"

    if failed_steps:
        prompt += f"\nFAILED STEPS ({len(failed_steps)} steps):\n"
        for s in failed_steps:
            prompt += f"- {s.get('name', s.get('tool_name', 'unknown'))}: {s.get('error', 'unknown error')}\n"

    # Hydrate active recovery plans
    active_plans = [
        p for p in snapshot.get("recovery_plans", [])
        if p.get("status") in ("PROPOSED", "APPROVED", "EXECUTING")
    ]
    if active_plans:
        prompt += f"\nACTIVE RECOVERY PLANS ({len(active_plans)}):\n"
        for p in active_plans:
            prompt += f"- Plan ID: {p.get('plan_id')}\n"
            prompt += f"  Target Outcome: {p.get('target_outcome_id')}\n"
            prompt += f"  Diagnosis: {p.get('diagnosis')}\n"
            prompt += f"  Status: {p.get('status')}\n"
            prompt += f"  Verification Strategy: {p.get('verification_strategy', '')}\n"
            steps = p.get("proposed_steps", [])
            if steps:
                prompt += f"  Proposed Steps:\n"
                for step in steps:
                    prompt += f"    * Tool: {step.get('tool_name')}, Args: {json.dumps(step.get('tool_args', {}))}\n"

    # Hydrate approved actions
    approved_actions = [
        a for a in snapshot.get("approvals", [])
        if a.get("status") == "APPROVED"
    ]
    if approved_actions:
        prompt += f"\nHUMAN APPROVALS GRANTED ({len(approved_actions)}):\n"
        for a in approved_actions:
            prompt += f"- Tool '{a.get('action_tool')}' with args {json.dumps(a.get('action_args', {}))} approved by {a.get('decided_by', 'operator')}: {a.get('decision_reason', '')}\n"

    prompt += "\nBegin executing. Achieve all required outcomes while respecting constraints."

    return prompt


# ---------------------------------------------------------------------------
# Startup Recovery
# ---------------------------------------------------------------------------


async def recover_incomplete_workflows():
    """Check for incomplete workflows, reconcile in-flight state, and resume them."""
    incomplete = await store.get_incomplete_workflows()
    for wf in incomplete:
        wf_id = wf["workflow_id"]
        # 1. Reconcile in-flight / interrupted state against external services
        await engine.reconcile_interrupted_workflow(wf_id, services)

        # 2. Check updated state
        updated_wf = await store.get_workflow(wf_id)
        if updated_wf and updated_wf.get("state") not in ("AWAITING_APPROVAL", "COMPLETED", "ESCALATED"):
            updated_wf["resumed_at"] = datetime.now(timezone.utc).isoformat()
            await store.save_workflow(updated_wf)
            await engine._record_event(
                workflow_id=wf_id,
                event_type=EventType.WORKFLOW_RESUMED,
                title="Workflow Resumed",
                detail="Server restarted — reconciled and resumed workflow",
                actor="system",
            )
            task = asyncio.create_task(_run_agent(wf_id))
            shutdown_manager.register_task(task)
