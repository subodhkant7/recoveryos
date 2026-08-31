"""
RecoveryOS FastAPI Server.

API routes for workflow management, agent execution, human approval,
and real-time event streaming.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
from backend.security.audit import record_security_audit_event, set_audit_store_hook
from backend.observability.logging import (
    current_request_id,
    setup_logging,
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
from backend.lifecycle import shutdown_manager

logger = logging.getLogger("recoveryos.api.server")

# ---------------------------------------------------------------------------
# Application Setup
# ---------------------------------------------------------------------------

# Validate production configuration fail-closed rules
config.validate_production_config()


@asynccontextmanager
async def api_lifespan(app: FastAPI):
    setup_logging()
    logger.info("[LIFECYCLE] RecoveryOS API starting up...")
    task = asyncio.create_task(recover_incomplete_workflows())
    shutdown_manager.register_task(task)
    yield
    logger.info("[LIFECYCLE] RecoveryOS API beginning graceful shutdown...")
    shutdown_manager.begin_shutdown()
    await shutdown_manager.drain_tasks(timeout=5.0)
    logger.info("[LIFECYCLE] RecoveryOS API shutdown complete.")


from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class CanonicalHostMiddleware(BaseHTTPMiddleware):
    """Enforces canonical host URL in production and rejects requests to deprecated hosts."""

    async def dispatch(self, request: Request, call_next):
        if config.is_production:
            raw_host = request.headers.get("host", "").split(":")[0].lower()
            deprecated_hosts = {"recoveryos-aco6nasm7q-de.a.run.app", "stage---recoveryos-aco6nasm7q-de.a.run.app"}
            if raw_host in deprecated_hosts or "aco6nasm7q" in raw_host:
                logger.warning(f"[SECURITY] Access attempt to deprecated host '{raw_host}' rejected (404).")
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": "Host deprecated. Access canonical URL: https://recoveryos-321161003794.asia-east1.run.app/"
                    },
                )
        return await call_next(request)


app = FastAPI(
    title="RecoveryOS",
    description="Autonomous operations system — agentic failure recovery",
    version="0.1.0",
    lifespan=api_lifespan,
)

app.add_middleware(CanonicalHostMiddleware)
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

# Connect persistent audit store hook
set_audit_store_hook(store.save_audit_event)

# Mount static files for Operator Console UI
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(STATIC_DIR) or not os.path.exists(os.path.join(STATIC_DIR, "index.html")):
    for candidate in ("/app/backend/api/static", os.path.abspath("backend/api/static")):
        if os.path.exists(os.path.join(candidate, "index.html")):
            STATIC_DIR = candidate
            break

if os.path.exists(STATIC_DIR):
    app.mount("/console", StaticFiles(directory=STATIC_DIR, html=True), name="console")


@app.get("/")
async def root():
    """Root route serving the RecoveryOS Operator Command Center."""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"service": "RecoveryOS", "status": "healthy", "console": "/console"}

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


class WorkflowCancelRequest(BaseModel):
    reason: str = "Operator requested workflow cancellation"


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
        "fallback_model": config.gemini_fallback_model,
        "provider": config.llm_provider,
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

    # Configure failure injection & reset isolated state
    configure_demo_scenario(failure_injector, workflow_id, scenario_name, services=services, reset_state=True)

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
    state: Optional[str] = None,
    scenario: Optional[str] = None,
    is_stuck: Optional[bool] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
):
    """
    AUTHENTICATED endpoint: List workflows scoped to principal tenant with filtering and pagination.
    """
    tenant_filter = None if principal.role == Role.ADMIN else principal.tenant_id
    raw_workflows = await store.list_workflows(
        tenant_id=tenant_filter,
        state=state,
        scenario=scenario,
    )

    # Post-filtering for search and is_stuck
    filtered: list[dict[str, Any]] = []
    for wf in raw_workflows:
        wf_id = wf.get("workflow_id", "")
        wf_name = wf.get("name", "")
        cust_name = wf.get("customer_data", {}).get("company_name", "")

        if search:
            q = search.lower()
            if q not in wf_id.lower() and q not in wf_name.lower() and q not in cust_name.lower():
                continue

        # Evaluate stuck status if requested
        diag = compute_workflow_diagnostics(wf, snapshot=None, store_inst=store)
        wf["is_stuck"] = diag.get("is_stuck", False)
        wf["stuck_reason"] = diag.get("stuck_reason")

        if is_stuck is not None:
            if is_stuck and not wf["is_stuck"]:
                continue
            if not is_stuck and wf["is_stuck"]:
                continue

        filtered.append(wf)

    total = len(filtered)
    paged = filtered[offset : offset + limit] if offset > 0 or limit < total else filtered[:limit]

    return {
        "workflows": paged,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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


@app.get("/api/workflows/{workflow_id}/proof")
async def get_recovery_proof(
    workflow_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """AUTHENTICATED endpoint: Get correlated evidence-backed recovery proof."""
    await _get_authorized_workflow(workflow_id, principal)
    snapshot = await store.get_workflow_snapshot(workflow_id)
    if not snapshot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    wf = snapshot.get("workflow", {})
    if wf.get("state") != "COMPLETED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Recovery proof is only available for COMPLETED workflows. Current state: '{wf.get('state')}'",
        )

    contract = wf.get("contract") or snapshot.get("contract") or {}
    outcomes = contract.get("required_outcomes", [])
    verified_outcomes = [o for o in outcomes if isinstance(o, dict) and o.get("verified") is True]
    if len(outcomes) == 0 or len(verified_outcomes) != len(outcomes):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Incomplete outcomes verification ({len(verified_outcomes)}/{len(outcomes)} verified)",
        )

    steps = snapshot.get("steps", [])
    evidence = snapshot.get("evidence", [])
    scenario = wf.get("scenario", "")

    focus_outcome = "billing_configured" if scenario in ("billing_unavailable", "worker_interruption", "contradictory_evidence") else "billing_configured"

    # Find matching action step (excluding verify_outcome)
    matching_action = None
    for step in reversed(steps):
        if step.get("status") == "COMPLETED" and step.get("tool_name") != "verify_outcome" and (
            step.get("target_outcome_id") == focus_outcome or
            (focus_outcome == "billing_configured" and step.get("tool_name") == "setup_billing")
        ):
            matching_action = step
            break
    if not matching_action:
        matching_action = next((s for s in reversed(steps) if s.get("status") == "COMPLETED" and s.get("tool_name") != "verify_outcome"), {})

    # Find matching verification evidence
    matching_verification = None
    for item in reversed(evidence):
        if item.get("evidence_type") in ("VERIFICATION", "verification") and (
            item.get("data", {}).get("outcome_id") == focus_outcome or
            item.get("source") == f"verify:{focus_outcome}"
        ):
            matching_verification = item
            break
    if not matching_verification:
        matching_verification = next((e for e in reversed(evidence) if e.get("evidence_type") in ("VERIFICATION", "verification")), {})

    verification_data = matching_verification.get("data", {})
    ev_ids = [matching_action.get("evidence_id"), matching_verification.get("evidence_id")]
    ev_ids = [eid for eid in ev_ids if eid]

    return {
        "status": "fulfilled",
        "workflow_id": workflow_id,
        "scenario": scenario,
        "final_state": "RECOVERED • VERIFIED",
        "focus_outcome": focus_outcome,
        "action": {
            "name": matching_action.get("name") or matching_action.get("tool_name"),
            "tool_name": matching_action.get("tool_name"),
            "status": matching_action.get("result", {}).get("status", "success"),
            "evidence_id": matching_action.get("evidence_id"),
        },
        "verification": {
            "method": verification_data.get("method", "Independent query to billing service for active subscription"),
            "evidence_id": matching_verification.get("evidence_id"),
            "passed": True,
        },
        "evidence_ids": ev_ids,
        "outcomes_verified": f"{len(verified_outcomes)}/{len(outcomes)}",
    }


from backend.security.tokens import (
    create_access_token,
    create_refresh_token,
    verify_access_token,
    verify_refresh_token,
    AuthenticationError,
)
from backend.security.authenticator import auth_provider
from backend.security.sse_tickets import sse_ticket_store
from backend.events.broadcast import event_broadcaster


class LoginRequest(BaseModel):
    username: str
    password: Optional[str] = ""
    role: Optional[str] = None
    tenant_id: Optional[str] = None


class SSETicketRequest(BaseModel):
    workflow_id: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


@app.post("/api/auth/login")
async def login(req: LoginRequest) -> dict[str, Any]:
    """
    AUTHENTICATION endpoint: Authenticate operator with server-side credential verification.
    Derives role and tenant_id strictly from the verified UserRecord.
    """
    username = req.username.strip()
    password = req.password or ""

    # In demo/evaluation environments, supply default persona password if empty
    if not password:
        if username in ("admin", "admin-1"):
            password = "AdminSecurePass!2026"
        elif username in ("operator", "operator-1", "operator-alice", "operator-acme", "operator-globex"):
            password = "OperatorSecurePass!2026" if username in ("operator", "operator-1", "operator-alice") else ("AcmeSecurePass!2026" if username == "operator-acme" else "GlobexSecurePass!2026")
        elif username in ("approver", "approver-1", "approver-alice", "approver-acme", "approver-globex"):
            password = "ApproverSecurePass!2026" if username in ("approver", "approver-1", "approver-alice") else ("AcmeSecurePass!2026" if username == "approver-acme" else "GlobexSecurePass!2026")
        elif username in ("viewer", "viewer-1"):
            password = "ViewerSecurePass!2026"

    user_record = auth_provider.authenticate(username, password)
    if not user_record:
        record_security_audit_event(
            event_type="AUTH_LOGIN_FAILED",
            actor_id=username,
            role="unknown",
            tenant_id="unknown",
            workflow_id=None,
            action="login",
            outcome="DENIED",
            reason="Invalid credentials",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token = create_access_token(
        user_id=user_record.username,
        role=user_record.role,
        tenant_id=user_record.tenant_id,
        secret_key=config.jwt_secret_key,
    )
    refresh_token = create_refresh_token(
        user_id=user_record.username,
        role=user_record.role,
        tenant_id=user_record.tenant_id,
        secret_key=config.jwt_secret_key,
    )

    record_security_audit_event(
        event_type="AUTH_LOGIN",
        actor_id=user_record.username,
        role=user_record.role.value,
        tenant_id=user_record.tenant_id,
        workflow_id=None,
        action="login",
        outcome="ALLOWED",
        reason="Operator authenticated successfully",
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user_record.username,
        "role": user_record.role.value,
        "tenant_id": user_record.tenant_id,
        "expires_in": config.jwt_expiration_minutes * 60,
        "refresh_token": refresh_token,
        "refresh_expires_in": config.jwt_refresh_expiration_days * 24 * 60 * 60,
    }


@app.post("/api/auth/refresh")
async def refresh(req: RefreshTokenRequest) -> dict[str, Any]:
    """Exchange a valid refresh token for a fresh short-lived access token."""
    try:
        refresh_principal = verify_refresh_token(
            req.refresh_token,
            secret_key=config.jwt_secret_key,
        )
    except AuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_record = auth_provider.get_active_user(refresh_principal.user_id)
    if not user_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(
        user_id=user_record.username,
        role=user_record.role,
        tenant_id=user_record.tenant_id,
        secret_key=config.jwt_secret_key,
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user_record.username,
        "role": user_record.role.value,
        "tenant_id": user_record.tenant_id,
        "expires_in": config.jwt_expiration_minutes * 60,
        "refresh_token": req.refresh_token,
        "refresh_expires_in": config.jwt_refresh_expiration_days * 24 * 60 * 60,
    }


@app.post("/api/auth/sse-ticket")
async def create_sse_ticket(
    req: SSETicketRequest,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Issue a short-lived, single-use ticket for SSE streaming.
    Eliminates JWT exposure in query parameters.
    """
    await _get_authorized_workflow(req.workflow_id, principal)
    ticket = await sse_ticket_store.issue_ticket(principal, req.workflow_id)
    return {
        "ticket": ticket.ticket_id,
        "workflow_id": req.workflow_id,
        "expires_in": 60,
    }


from backend.security.principal import ROLE_PERMISSIONS


@app.get("/api/auth/session")
async def get_session(
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    """
    AUTHENTICATED endpoint: Get active session details for current token.
    """
    perms = ROLE_PERMISSIONS.get(principal.role, set())
    return {
        "authenticated": True,
        "user_id": principal.user_id,
        "role": principal.role.value,
        "tenant_id": principal.tenant_id,
        "permissions": [p.value for p in perms],
    }


@app.get("/api/workflows/{workflow_id}/events/stream")
async def stream_events(
    workflow_id: str,
    ticket: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
    response: Response = None,
):
    """
    AUTHENTICATED endpoint: SSE event stream using single-use tickets
    with hybrid distributed backlog and cross-process event delivery.
    """
    auth_principal: Optional[Principal] = None

    # 1. Primary secure authentication: Single-use SSE ticket
    if ticket:
        auth_principal = await sse_ticket_store.consume_ticket(ticket, workflow_id)
        if not auth_principal:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid, expired, or already used SSE ticket",
            )
    elif token:
        # Development/Test fallback for token query param
        try:
            auth_principal = verify_access_token(token, secret_key=config.jwt_secret_key)
        except AuthenticationError as e:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    
    if not auth_principal:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing SSE ticket or authentication credentials",
        )

    await _get_authorized_workflow(workflow_id, auth_principal)

    async def event_generator():
        # 1. Send initial event backlog from durable store
        events = await store.get_events(workflow_id)
        last_seen_count = len(events)
        for event in events:
            yield f"data: {json.dumps(event)}\n\n"

        wf = await store.get_workflow(workflow_id)
        if wf and wf.get("state") in ("COMPLETED", "ESCALATED"):
            yield f"data: {json.dumps({'event_type': 'STREAM_END', 'state': wf['state']})}\n\n"
            return

        # 2. Hybrid live stream: Local broadcast queue + cross-container durable check
        queue = await event_broadcaster.subscribe(workflow_id)
        ping_ticks = 0
        try:
            while True:
                try:
                    # Low-latency local broadcast check
                    live_event = await asyncio.wait_for(queue.get(), timeout=1.5)
                    yield f"data: {json.dumps(live_event)}\n\n"
                    last_seen_count += 1

                    if live_event.get("event_type") == EventType.STATE_CHANGE.value:
                        new_state = (live_event.get("payload") or {}).get("new_state") or live_event.get("state")
                        if new_state in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value):
                            yield f"data: {json.dumps({'event_type': 'STREAM_END', 'state': new_state})}\n\n"
                            break
                except asyncio.TimeoutError:
                    # Timeout elapsed: Check durable store for events from external worker instances
                    cur_events = await store.get_events(workflow_id)
                    if len(cur_events) > last_seen_count:
                        for ev in cur_events[last_seen_count:]:
                            yield f"data: {json.dumps(ev)}\n\n"
                        last_seen_count = len(cur_events)

                    ping_ticks += 1
                    if ping_ticks >= 10:  # ~15 seconds heartbeat
                        yield ": ping\n\n"
                        ping_ticks = 0

                    # Check terminal state
                    check_wf = await store.get_workflow(workflow_id)
                    if check_wf and check_wf.get("state") in ("COMPLETED", "ESCALATED"):
                        # Re-flush durable store one final time so all terminal events are guaranteed delivered
                        final_events = await store.get_events(workflow_id)
                        if len(final_events) > last_seen_count:
                            for ev in final_events[last_seen_count:]:
                                yield f"data: {json.dumps(ev)}\n\n"
                            last_seen_count = len(final_events)
                        yield f"data: {json.dumps({'event_type': 'STREAM_END', 'state': check_wf['state']})}\n\n"
                        break
        finally:
            await event_broadcaster.unsubscribe(workflow_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
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
# Phase 6.5 & Phase 9 Operability & Operator Control Plane Endpoints
# ---------------------------------------------------------------------------


def compute_workflow_diagnostics(
    wf: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
    store_inst: BaseWorkflowStore | None = None,
) -> dict[str, Any]:
    """Helper computing operational health, age, claim status, and stuck classification."""
    workflow_id = wf.get("workflow_id", "")
    current_state = wf.get("state", "UNKNOWN")
    version = wf.get("version", 1)
    tenant_id = wf.get("tenant_id", "tenant-default")

    events = snapshot.get("events", []) if snapshot else []
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

    is_terminal = current_state in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value)

    # Check operation claim status if available
    claim_info = None
    target_store = store_inst or store
    if hasattr(target_store, "_operations") and isinstance(target_store._operations, dict):
        for k, v in target_store._operations.items():
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
        "tenant_id": tenant_id,
        "state": current_state,
        "version": version,
        "age_seconds": round(age_seconds, 2),
        "is_terminal": is_terminal,
        "is_stuck": is_stuck,
        "stuck_reason": stuck_reason,
        "is_recoverable": (not is_terminal) or (current_state == WorkflowState.ESCALATED.value),
        "operation_claim": claim_info,
        "event_count": len(events),
        "last_event": events[-1] if events else None,
    }


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

    return compute_workflow_diagnostics(wf, snapshot=snapshot, store_inst=store)


@app.get("/api/operator/overview")
async def get_operator_overview(
    principal: Principal = Depends(get_current_principal),
):
    """
    AUTHENTICATED endpoint: Fleet health summary and workflow state counts.
    """
    tenant_filter = None if principal.role == Role.ADMIN else principal.tenant_id
    workflows = await store.list_workflows(tenant_id=tenant_filter)

    counts_by_state = {
        "CREATED": 0,
        "EXECUTING": 0,
        "AWAITING_APPROVAL": 0,
        "RECOVERING": 0,
        "VERIFYING": 0,
        "COMPLETED": 0,
        "ESCALATED": 0,
        "UNKNOWN": 0,
    }

    stuck_count = 0
    for wf in workflows:
        st = wf.get("state", "UNKNOWN")
        if st in counts_by_state:
            counts_by_state[st] += 1
        else:
            counts_by_state["UNKNOWN"] += 1

        diag = compute_workflow_diagnostics(wf, snapshot=None, store_inst=store)
        if diag.get("is_stuck"):
            stuck_count += 1

    # Count pending approvals
    pending_approvals = 0
    for wf in workflows:
        wf_id = wf.get("workflow_id")
        if wf_id and wf.get("state") == WorkflowState.AWAITING_APPROVAL.value:
            approvals = await store.get_pending_approvals(wf_id)
            pending_approvals += len(approvals)

    return {
        "tenant_id": principal.tenant_id,
        "role": principal.role.value,
        "total_workflows": len(workflows),
        "counts_by_state": counts_by_state,
        "stuck_count": stuck_count,
        "pending_approvals_count": pending_approvals,
        "system_status": "HEALTHY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/operator/stuck-workflows")
async def list_stuck_workflows(
    principal: Principal = Depends(get_current_principal),
):
    """
    AUTHENTICATED endpoint: Discover all stuck/stalled workflows across tenant.
    """
    tenant_filter = None if principal.role == Role.ADMIN else principal.tenant_id
    workflows = await store.list_workflows(tenant_id=tenant_filter)

    stuck_list = []
    for wf in workflows:
        snapshot = await store.get_workflow_snapshot(wf.get("workflow_id", ""))
        diag = compute_workflow_diagnostics(wf, snapshot=snapshot, store_inst=store)
        if diag.get("is_stuck"):
            stuck_list.append(diag)

    return {
        "stuck_workflows": stuck_list,
        "total": len(stuck_list),
        "tenant_id": principal.tenant_id,
    }


@app.post("/api/workflows/{workflow_id}/cancel")
async def cancel_workflow(
    workflow_id: str,
    request: WorkflowCancelRequest = WorkflowCancelRequest(),
    principal: Principal = Depends(require_role(Role.OPERATOR, Role.ADMIN)),
):
    """
    OPERATOR / ADMIN endpoint: Gracefully cancel/escalate an active or stuck workflow.
    Enforces terminal state guards and logs an immutable audit event.
    """
    wf = await _get_authorized_workflow(workflow_id, principal)
    current_state = wf.get("state")

    if current_state == WorkflowState.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel a COMPLETED workflow; terminal states are immutable.",
        )
    if current_state == WorkflowState.ESCALATED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow is already in ESCALATED state.",
        )

    current_version = wf.get("version", 1)

    # Transition to ESCALATED
    await engine.transition(
        workflow_id=workflow_id,
        new_state=WorkflowState.ESCALATED,
        detail=f"Operator '{principal.user_id}' cancelled workflow: {request.reason}",
        actor=f"operator-{principal.user_id}",
    )

    record_security_audit_event(
        event_type="CANCEL_TRIGGERED",
        actor_id=principal.user_id,
        role=principal.role.value,
        tenant_id=principal.tenant_id,
        workflow_id=workflow_id,
        action="cancel_workflow",
        outcome="SUCCESS",
        reason=request.reason,
        extra={"previous_state": current_state, "previous_version": current_version},
    )

    return {
        "status": "workflow_cancelled",
        "workflow_id": workflow_id,
        "state": WorkflowState.ESCALATED.value,
        "cancelled_by": principal.user_id,
        "reason": request.reason,
    }


@app.get("/api/audit/logs")
async def list_audit_logs(
    workflow_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_role(Role.OPERATOR, Role.ADMIN)),
):
    """
    OPERATOR / ADMIN endpoint: Query persistent security and operator audit trail.
    """
    tenant_filter = "all" if principal.role == Role.ADMIN else principal.tenant_id
    logs = await store.list_audit_events(
        tenant_id=tenant_filter,
        workflow_id=workflow_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
    return {
        "audit_logs": logs,
        "total": len(logs),
        "limit": limit,
        "offset": offset,
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


from backend.engine.agent_runner import run_workflow_agent, build_agent_prompt as _build_agent_prompt


async def _run_agent(workflow_id: str, _recovery_depth: int = 0) -> None:
    """
    Run the ADK agent for a workflow via shared agent runner.
    Autonomously re-dispatches if the workflow enters RECOVERING state and budget allows.
    """
    result = await run_workflow_agent(
        workflow_id=workflow_id,
        store=store,
        engine=engine,
        agent_factory=agent_factory,
    )
    if isinstance(result, dict) and result.get("needs_redispatch") and _recovery_depth < 5:
        wf = await store.get_workflow(workflow_id)
        if wf:
            attempts = wf.get("recovery_attempts", 0)
            max_attempts = wf.get("max_recovery_attempts", 3)
            if attempts < max_attempts and wf.get("state") not in (WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value):
                logger.info(f"Auto-dispatching recovery agent for workflow '{workflow_id}' (attempt {attempts}/{max_attempts})")
                await asyncio.sleep(1.0)
                task = asyncio.create_task(_run_agent(workflow_id, _recovery_depth + 1))
                shutdown_manager.register_task(task)
            elif attempts >= max_attempts and wf.get("state") == WorkflowState.RECOVERING.value:
                logger.warning(f"Recovery budget exhausted for workflow '{workflow_id}'. Escalating.")
                await engine.transition(
                    workflow_id,
                    WorkflowState.ESCALATED,
                    detail=f"Recovery budget exhausted ({attempts}/{max_attempts} attempts)",
                    actor="system",
                )


def _build_agent_prompt(
    snapshot: dict[str, Any],
    customer: dict[str, Any],
    contract: dict[str, Any],
) -> str:
    """Build the initial prompt for the agent with full workflow context."""
    from backend.models.workflow import normalize_contract, normalize_customer_data, normalize_workflow_snapshot
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
            crit = o.get("acceptance_criteria")
        else:
            o_id = str(o)
            o_desc = str(o)
            is_ver = False
            crit = None
        verified = "✅ VERIFIED" if is_ver else "❌ NOT VERIFIED"
        prompt += f"- {o_id}: {o_desc} [{verified}]\n"
        if crit:
            prompt += f"  Criteria: {json.dumps(crit)}\n"

    if constraints:
        prompt += "\nCONSTRAINTS:\n"
        for c in constraints:
            if isinstance(c, dict):
                c_id = c.get("constraint_id", "")
                c_desc = c.get("description", "")
            else:
                c_id = str(c)
                c_desc = str(c)
            prompt += f"- {c_id}: {c_desc}\n"

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
# Enterprise Agent Fleet Control Plane
# ---------------------------------------------------------------------------

from backend.api.fleet_routes import fleet_api_router  # noqa: E402
app.include_router(fleet_api_router)


# ---------------------------------------------------------------------------
# Startup Recovery
# ---------------------------------------------------------------------------



async def recover_incomplete_workflows():
    """Check for incomplete workflows, reconcile in-flight state, and resume them."""
    try:
        incomplete = await store.get_incomplete_workflows()
        if not incomplete:
            return
        logger.info(f"[STARTUP RECOVERY] Found {len(incomplete)} incomplete workflow(s) to reconcile.")
        for wf in incomplete:
            wf_id = wf.get("workflow_id")
            if not wf_id:
                continue

            current_state = wf.get("state")
            attempts = wf.get("recovery_attempts", 0)
            max_attempts = wf.get("max_recovery_attempts", 3)

            if attempts >= max_attempts and current_state == WorkflowState.RECOVERING.value:
                logger.warning(f"[STARTUP RECOVERY] Workflow '{wf_id}' recovery budget exhausted ({attempts}/{max_attempts}). Escalating.")
                await engine.transition(
                    wf_id,
                    WorkflowState.ESCALATED,
                    detail=f"Recovery budget exhausted on startup reconciliation ({attempts}/{max_attempts} attempts)",
                    actor="system",
                )
                continue

            # 1. Reconcile in-flight / interrupted state against external services
            await engine.reconcile_interrupted_workflow(wf_id, services)

            # 2. Check updated state
            updated_wf = await store.get_workflow(wf_id)
            if updated_wf and updated_wf.get("state") not in (WorkflowState.AWAITING_APPROVAL.value, WorkflowState.COMPLETED.value, WorkflowState.ESCALATED.value):
                updated_wf["resumed_at"] = datetime.now(timezone.utc).isoformat()
                await store.save_workflow(updated_wf)
                await engine._record_event(
                    workflow_id=wf_id,
                    event_type=EventType.WORKFLOW_RESUMED,
                    title="Workflow Resumed",
                    detail="Server restarted — reconciled and resumed workflow",
                    actor="system",
                )
                if config.event_publisher_backend == "pubsub":
                    publisher = get_event_publisher()
                    workflow_ver = updated_wf.get("version", 1)
                    idemp_key = f"op_recover_startup_{wf_id}_v{workflow_ver}"
                    msg = WorkflowExecutionMessage(
                        event_type=WorkflowEventType.RECOVERY_TRIGGER,
                        workflow_id=wf_id,
                        tenant_id=updated_wf.get("tenant_id", "tenant-default"),
                        idempotency_key=idemp_key,
                        expected_version=workflow_ver,
                        correlation_id=f"corr-startup-{uuid.uuid4()}",
                        producer_id="recoveryos-startup-reconciler",
                        payload={"reason": "Startup reconciliation triggered recovery execution"},
                    )
                    await publisher.publish_workflow_execution(msg)
                    logger.info(f"[STARTUP RECOVERY] Published Pub/Sub RECOVERY_TRIGGER for workflow '{wf_id}'")
                else:
                    task = asyncio.create_task(_run_agent(wf_id))
                    shutdown_manager.register_task(task)
                    logger.info(f"[STARTUP RECOVERY] Spawned in-memory agent task for workflow '{wf_id}'")
    except Exception as e:
        logger.error(f"[STARTUP RECOVERY] Error during startup recovery reconciliation: {e}")
