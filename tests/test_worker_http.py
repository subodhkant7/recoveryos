"""
Phase 6.2.5: Worker HTTP Endpoint & Pub/Sub Push Ingress Test Suite.

Comprehensive tests covering:
- Health and Readiness endpoints
- Valid Pub/Sub push envelope unpacking & base64 decoding
- Valid workflow message execution -> HTTP 200 (ACK)
- Malformed JSON / missing envelope fields -> HTTP 400
- Poison pill / invalid message schema -> HTTP 422 (DEAD_LETTER)
- Security provenance & tenant mismatch rejection -> HTTP 422 (DEAD_LETTER)
- Transient / OCC conflict -> HTTP 500 (NACK)
- Duplicate redelivery -> HTTP 200 (ACK)
- Terminal workflow -> HTTP 200 (ACK)
- Correlation ID propagation and secret redaction
"""

import base64
import json
import uuid
from datetime import datetime, timezone
from typing import Any
import pytest
from httpx import AsyncClient, ASGITransport

from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
)
from backend.events.consumer import WorkflowEventConsumer
from backend.engine.workflow_engine import WorkflowEngine
from backend.models import (
    WorkflowState,
    OutcomeContract,
    RequiredOutcome,
)
from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
from backend.worker.server import app, set_worker_service
from backend.worker.service import WorkflowWorkerService
from backend.worker.security import DefaultWorkerSecurityValidator


def _make_pubsub_envelope(message_obj: WorkflowExecutionMessage | dict[str, Any] | str) -> dict[str, Any]:
    """Helper to wrap payload into standard Google Cloud Pub/Sub push envelope."""
    if isinstance(message_obj, WorkflowExecutionMessage):
        payload_str = message_obj.to_pubsub_json()
    elif isinstance(message_obj, dict):
        payload_str = json.dumps(message_obj)
    else:
        payload_str = str(message_obj)

    b64_data = base64.b64encode(payload_str.encode("utf-8")).decode("utf-8")
    return {
        "message": {
            "attributes": {"source": "pubsub-test"},
            "data": b64_data,
            "messageId": f"msg-pubsub-{uuid.uuid4()}",
            "publishTime": datetime.now(timezone.utc).isoformat(),
        },
        "subscription": "projects/recoveryos-506713/subscriptions/recoveryos-workflow-execution-worker",
    }


async def _create_test_workflow(engine, tenant_id="tenant-acme", name="Worker Test WF"):
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    return await engine.create_workflow(
        name=name,
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id=tenant_id,
    )


@pytest.fixture
async def worker_client():
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    consumer = WorkflowEventConsumer(store=store, engine=engine)
    validator = DefaultWorkerSecurityValidator()
    service = WorkflowWorkerService(consumer=consumer, security_validator=validator, worker_id="test-worker")
    set_worker_service(service)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store, engine, service


# ===========================================================================
# 1. Health & Readiness Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_01_health_and_readiness_probes(worker_client):
    client, _, _, _ = worker_client

    h_res = await client.get("/health")
    assert h_res.status_code == 200
    assert h_res.json()["status"] == "HEALTHY"
    assert h_res.json()["service"] == "recoveryos-worker"

    r_res = await client.get("/readiness")
    assert r_res.status_code == 200
    assert r_res.json()["status"] == "READY"


# ===========================================================================
# 2. Envelope & Base64 Decoding Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_02_valid_pubsub_envelope_processing(worker_client):
    client, store, engine, _ = worker_client
    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        message_id=str(uuid.uuid4()),
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-acme",
        workflow_id=wf_id,
        idempotency_key="idemp-wf-01",
        expected_version=1,
        producer_id="recoveryos-api",
    )

    envelope = _make_pubsub_envelope(msg)
    res = await client.post("/", json=envelope)

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ACK"
    assert body["workflow_id"] == wf_id

    updated_wf = await store.get_workflow(wf_id)
    assert updated_wf["state"] == WorkflowState.EXECUTING.value


@pytest.mark.asyncio
async def test_03_malformed_json_envelope_rejected(worker_client):
    client, _, _, _ = worker_client

    # Send empty body / invalid JSON
    res = await client.post("/", content=b"{invalid-json")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_04_missing_message_field_rejected(worker_client):
    client, _, _, _ = worker_client

    res = await client.post("/", json={"subscription": "some-sub"})
    assert res.status_code == 400
    assert "Missing 'message' field" in res.json()["error"]


@pytest.mark.asyncio
async def test_05_invalid_base64_data_rejected(worker_client):
    client, _, _, _ = worker_client

    bad_envelope = {
        "message": {"data": "not-valid-base64%%"},
        "subscription": "sub",
    }
    res = await client.post("/", json=bad_envelope)
    assert res.status_code == 422


# ===========================================================================
# 3. Poison Pill & Security Provenance Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_06_malformed_message_payload_dead_letter(worker_client):
    client, _, _, _ = worker_client

    envelope = _make_pubsub_envelope('{"malformed": true, "no_contract": 123}')
    res = await client.post("/", json=envelope)
    assert res.status_code == 422
    assert res.json()["status"] == "DEAD_LETTER"


@pytest.mark.asyncio
async def test_07_untrusted_producer_identity_dead_letter(worker_client):
    client, _, _, _ = worker_client

    bad_msg = {
        "schema_version": "1.0.0",
        "message_id": str(uuid.uuid4()),
        "event_type": "WORKFLOW_DISPATCH",
        "workflow_id": str(uuid.uuid4()),
        "tenant_id": "tenant-attacker",
        "idempotency_key": "idemp-evil",
        "producer_id": "evil-hacker-daemon",
        "expected_version": 1,
    }

    envelope = _make_pubsub_envelope(bad_msg)
    res = await client.post("/", json=envelope)
    assert res.status_code == 422
    assert res.json()["status"] == "DEAD_LETTER"
    assert "Security verification failed" in res.json()["error"]


@pytest.mark.asyncio
async def test_08_tenant_mismatch_dead_letter(worker_client):
    client, store, engine, _ = worker_client

    wf = await _create_test_workflow(engine, tenant_id="tenant-victim")
    wf_id = wf["workflow_id"]

    # Message specifies tenant-attacker for tenant-victim workflow
    msg = WorkflowExecutionMessage(
        message_id=str(uuid.uuid4()),
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-attacker",
        workflow_id=wf_id,
        idempotency_key="idemp-tenant-mis",
        expected_version=1,
        producer_id="recoveryos-api",
    )

    envelope = _make_pubsub_envelope(msg)
    res = await client.post("/", json=envelope)
    assert res.status_code == 422
    assert res.json()["status"] == "DEAD_LETTER"


# ===========================================================================
# 4. Concurrency, OCC Conflict & Redelivery Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_09_stale_occ_version_causes_nack_retry(worker_client):
    client, store, engine, _ = worker_client

    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Transitioned")

    # Workflow is now at version 2, message expects version 1
    msg = WorkflowExecutionMessage(
        message_id=str(uuid.uuid4()),
        event_type=WorkflowEventType.STEP_EXECUTE,
        schema_version="1.0.0",
        tenant_id="tenant-acme",
        workflow_id=wf_id,
        idempotency_key="idemp-stale-01",
        expected_version=1,
        producer_id="recoveryos-api",
    )

    envelope = _make_pubsub_envelope(msg)
    res = await client.post("/", json=envelope)

    assert res.status_code == 500  # NACK for redelivery
    assert res.json()["status"] == "NACK"


@pytest.mark.asyncio
async def test_10_duplicate_redelivery_ack(worker_client):
    client, store, engine, _ = worker_client

    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        message_id="msg-dup-01",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-acme",
        workflow_id=wf_id,
        idempotency_key="idemp-dup-01",
        expected_version=1,
        producer_id="recoveryos-api",
    )
    envelope = _make_pubsub_envelope(msg)

    # First delivery -> ACK
    res1 = await client.post("/", json=envelope)
    assert res1.status_code == 200
    assert res1.json()["status"] == "ACK"

    # Duplicate redelivery -> ACK cleanly
    res2 = await client.post("/", json=envelope)
    assert res2.status_code == 200
    assert res2.json()["status"] == "ACK"


@pytest.mark.asyncio
async def test_11_terminal_workflow_dropped_ack(worker_client):
    client, store, engine, _ = worker_client

    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    # Mark completed in store
    wf_obj = await store.get_workflow(wf_id)
    wf_obj["state"] = WorkflowState.COMPLETED.value
    await store.save_workflow(wf_obj, expected_version=1)

    msg = WorkflowExecutionMessage(
        message_id=str(uuid.uuid4()),
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-acme",
        workflow_id=wf_id,
        idempotency_key="idemp-term-01",
        expected_version=2,
        producer_id="recoveryos-api",
    )

    envelope = _make_pubsub_envelope(msg)
    res = await client.post("/", json=envelope)

    assert res.status_code == 200
    assert res.json()["status"] == "ACK"


# ===========================================================================
# 5. Correlation ID & Observability Propagation
# ===========================================================================

@pytest.mark.asyncio
async def test_12_correlation_id_and_header_propagation(worker_client):
    client, store, engine, _ = worker_client

    wf = await _create_test_workflow(engine)
    wf_id = wf["workflow_id"]

    msg = WorkflowExecutionMessage(
        message_id=str(uuid.uuid4()),
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-acme",
        workflow_id=wf_id,
        idempotency_key="idemp-corr-01",
        expected_version=1,
        correlation_id="corr-trace-http-999",
        producer_id="recoveryos-api",
    )

    envelope = _make_pubsub_envelope(msg)
    res = await client.post("/", json=envelope, headers={"X-Correlation-ID": "corr-trace-http-999"})

    assert res.status_code == 200
    assert res.headers.get("x-request-id") == "corr-trace-http-999"
