"""
Dedicated Cloud Run Worker HTTP Entrypoint for RecoveryOS Asynchronous Execution.

Receives authenticated Google Cloud Pub/Sub push envelopes, unpacks messages,
enforces security boundaries, executes workflow operations, and translates results
to HTTP status codes for Pub/Sub ACK / NACK / DLQ handling.
"""

from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.config import config
from backend.events.consumer import WorkflowEventConsumer
from backend.events.message_models import MessageValidationError
from backend.lifecycle import shutdown_manager
from backend.observability.logging import (
    current_request_id,
    redact_sensitive_data,
    setup_logging,
)
from backend.observability.middleware import CorrelationAndMetricsMiddleware
from backend.persistence.workflow_store import (
    BaseWorkflowStore,
    create_workflow_store,
)
from backend.worker.models import DeliveryStatus, FailureClassification, WorkerExecutionResult
from backend.worker.security import DefaultWorkerSecurityValidator
from backend.worker.service import WorkflowWorkerService


setup_logging()
logger = logging.getLogger("recoveryos.worker.server")

_store: BaseWorkflowStore | None = None
_worker_service: WorkflowWorkerService | None = None


def get_store() -> BaseWorkflowStore:
    global _store
    if _store is None:
        _store = create_workflow_store()
    return _store


from backend.engine.workflow_engine import WorkflowEngine
from backend.engine.policy_engine import PolicyEngine
from backend.simulation.failure_injector import FailureInjector
from backend.simulation.external_services import SimulatedServices
from backend.agents.agent_factory import AgentFactory
from backend.events.publisher import create_event_publisher


def get_worker_service() -> WorkflowWorkerService:
    global _worker_service
    if _worker_service is None:
        store = get_store()
        engine = WorkflowEngine(store)
        injector = FailureInjector()
        services = SimulatedServices(injector)
        policy_engine = PolicyEngine()
        agent_factory = AgentFactory(store, engine, services, policy_engine)
        event_publisher = create_event_publisher(config.event_publisher_backend)
        consumer = WorkflowEventConsumer(
            store=store,
            engine=engine,
            agent_factory=agent_factory,
            event_publisher=event_publisher,
            worker_id=f"worker-{config.environment}",
        )
        validator = DefaultWorkerSecurityValidator()
        _worker_service = WorkflowWorkerService(
            consumer=consumer,
            security_validator=validator,
            shutdown_manager=shutdown_manager,
            worker_id=f"worker-{config.environment}",
        )
    return _worker_service


def set_worker_service(service: WorkflowWorkerService | None) -> None:
    """Setter for dependency injection during unit and integration tests."""
    global _worker_service
    _worker_service = service


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "RecoveryOS Worker Service starting up",
        extra={"event_name": "WORKER_STARTUP", "environment": config.environment},
    )
    yield
    logger.info(
        "RecoveryOS Worker Service shutting down",
        extra={"event_name": "WORKER_SHUTDOWN", "environment": config.environment},
    )
    await shutdown_manager.drain()


app = FastAPI(
    title="RecoveryOS Asynchronous Worker",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(CorrelationAndMetricsMiddleware)


# ---------------------------------------------------------------------------
# Health & Readiness Probes
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    return {
        "status": "HEALTHY",
        "service": "recoveryos-worker",
        "environment": config.environment,
    }


@app.get("/readiness")
@app.get("/api/readiness")
async def readiness_check() -> dict[str, Any]:
    store = get_store()
    is_ready = True
    details = {
        "store_backend": config.persistence_backend,
        "store_type": type(store).__name__,
        "project_id": getattr(store, "_project_id", None),
        "database": getattr(store, "_database", None),
        "config_database": config.firestore_database,
        "config_env": config.environment,
    }

    if config.persistence_backend == "firestore" and not config.google_cloud_project:
        is_ready = False
        details["error"] = "GOOGLE_CLOUD_PROJECT is required for firestore"

    if not is_ready:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "NOT_READY", "details": details},
        )

    return {
        "status": "READY",
        "service": "recoveryos-worker",
        "details": details,
    }


@app.get("/debug/workflow/{workflow_id}")
async def debug_workflow(workflow_id: str):
    store = get_store()
    try:
        wf = await store.get_workflow(workflow_id)
        return {
            "found": wf is not None,
            "workflow": wf,
            "store_type": type(store).__name__,
            "store_project": getattr(store, "_project_id", None),
            "store_database": getattr(store, "_database", None),
        }
    except Exception as e:
        return {"error": str(e), "type": type(e).__name__}


@app.post("/debug/create_test_workflow")
async def create_test_workflow():
    import uuid
    store = get_store()
    wf_id = f"wf-worker-test-{uuid.uuid4().hex[:6]}"
    await store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": "RUNNING",
        "version": 1,
    })
    wf = await store.get_workflow(wf_id)
    return {"created": True, "workflow": wf}


# ---------------------------------------------------------------------------
# Pub/Sub Push Ingress Envelope Model
# ---------------------------------------------------------------------------

class PubSubMessagePayload(BaseModel):
    attributes: dict[str, str] = Field(default_factory=dict)
    data: str  # Base64 encoded payload
    messageId: str = ""
    publishTime: str = ""


class PubSubPushEnvelope(BaseModel):
    message: PubSubMessagePayload
    subscription: str = ""


# ---------------------------------------------------------------------------
# Pub/Sub Push Handler
# ---------------------------------------------------------------------------

@app.post("/")
@app.post("/pubsub/push")
@app.post("/api/worker/pubsub")
async def handle_pubsub_push(request: Request) -> Response:
    """
    Handle Google Cloud Pub/Sub push HTTP delivery.
    """
    # 1. Parse JSON body
    try:
        body = await request.json()
    except Exception as e:
        redacted_err = str(redact_sensitive_data(str(e)))
        logger.error(
            "Malformed JSON envelope received by worker",
            extra={"event_name": "WORKER_INVALID_JSON_ENVELOPE", "error": redacted_err},
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Invalid JSON body"},
        )

    if not isinstance(body, dict) or "message" not in body or not isinstance(body["message"], dict):
        logger.error(
            "Push payload does not match PubSubPushEnvelope schema",
            extra={"event_name": "WORKER_SCHEMA_VIOLATION"},
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Missing 'message' field in Pub/Sub envelope"},
        )

    msg_data_b64 = body["message"].get("data")
    if not msg_data_b64:
        logger.error(
            "Pub/Sub message data field is empty",
            extra={"event_name": "WORKER_EMPTY_DATA"},
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Missing 'data' field in message object"},
        )

    # 2. Base64 decode message data
    try:
        raw_payload_bytes = base64.b64decode(msg_data_b64)
    except Exception as e:
        logger.error(
            "Base64 decoding failed on message data",
            extra={"event_name": "WORKER_B64_DECODE_FAILED", "error": str(e)},
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "Base64 decode failed for message data"},
        )

    # 3. Process message through WorkflowWorkerService
    worker_service = get_worker_service()
    result = await worker_service.process_raw_payload(raw_payload_bytes)

    logger.info(
        f"Worker processing decision: {result.delivery_status.value}",
        extra={
            "event_name": "WORKER_DELIVERY_DECISION",
            "delivery_status": result.delivery_status.value,
            "failure_type": result.failure_type.value if result.failure_type else None,
            "workflow_id": result.workflow_id,
            "message_id": result.message_id,
            "tenant_id": result.tenant_id,
        },
    )

    # 4. Map Delivery Decision to HTTP Status Code
    if result.delivery_status == DeliveryStatus.ACK:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ACK",
                "workflow_id": result.workflow_id,
                "message_id": result.message_id,
                "tenant_id": result.tenant_id,
                "details": result.details,
            },
        )
    elif result.delivery_status == DeliveryStatus.NACK:
        # Return 500 so Pub/Sub will retry
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "NACK",
                "workflow_id": result.workflow_id,
                "message_id": result.message_id,
                "error": result.error_message,
            },
        )
    else:  # DEAD_LETTER (Permanent poison failure)
        # Return 422 so Pub/Sub counts as failure attempt and routes to DLQ per policy
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "status": "DEAD_LETTER",
                "workflow_id": result.workflow_id,
                "message_id": result.message_id,
                "error": result.error_message,
            },
        )
