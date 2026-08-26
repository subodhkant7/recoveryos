"""
Phase 6.2.1: Pub/Sub Message Contract & Ingestion Unit Test Suite.

Comprehensive tests covering message schema validation, serialization, deserialization,
deduplication, OCC enforcement, tenant isolation, and engine delegation.
"""

import json
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from pydantic import ValidationError

from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
    MessageValidationError,
)
from backend.events.publisher import (
    InMemoryEventPublisher,
    EventPublishError,
)
from backend.events.consumer import (
    WorkflowEventConsumer,
    ConsumerExecutionError,
)
from backend.models.workflow import WorkflowState
from backend.persistence.workflow_store import InMemoryWorkflowStore, StaleWorkflowStateError
from backend.engine.workflow_engine import WorkflowEngine
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract


@pytest.fixture
def store():
    return InMemoryWorkflowStore()


@pytest.fixture
def engine(store):
    return WorkflowEngine(store)


@pytest.fixture
def consumer(store, engine):
    return WorkflowEventConsumer(store, engine, worker_id="test-worker-1")


@pytest.fixture
def publisher():
    return InMemoryEventPublisher()


# ===========================================================================
# 1. Message Model & Serialization Tests
# ===========================================================================

def test_msg_01_valid_message_creation():
    """Verify that a well-formed WorkflowExecutionMessage initializes with default fields."""
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id="wf-test-101",
        tenant_id="tenant-alpha",
        idempotency_key="op_dispatch_wf-test-101",
        expected_version=1,
        payload={"scenario": "billing_unavailable"},
    )
    assert msg.schema_version == "1.0.0"
    assert msg.event_type == WorkflowEventType.WORKFLOW_DISPATCH
    assert msg.workflow_id == "wf-test-101"
    assert msg.tenant_id == "tenant-alpha"
    assert msg.expected_version == 1
    assert len(msg.message_id) > 10
    assert len(msg.correlation_id) > 10


def test_msg_02_serialization_and_deserialization():
    """Verify lossless serialization to JSON and attributes, and deserialization back."""
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.APPROVAL_RESUME,
        workflow_id="wf-test-102",
        tenant_id="tenant-beta",
        idempotency_key="op_resume_wf-test-102",
        expected_version=3,
        producer_id="recoveryos-api-asia",
        payload={"approval_id": "appr-555"},
        context={"actor": "admin"},
    )

    json_str = msg.to_pubsub_json()
    attributes = msg.to_pubsub_attributes()

    assert attributes["event_type"] == "APPROVAL_RESUME"
    assert attributes["expected_version"] == "3"
    assert attributes["tenant_id"] == "tenant-beta"

    rehydrated = WorkflowExecutionMessage.from_pubsub_json(json_str)
    assert rehydrated.message_id == msg.message_id
    assert rehydrated.workflow_id == msg.workflow_id
    assert rehydrated.expected_version == 3
    assert rehydrated.payload["approval_id"] == "appr-555"
    assert rehydrated.context["actor"] == "admin"


def test_msg_03_from_bytes_deserialization():
    """Verify from_pubsub_json handles utf-8 byte streams from Pub/Sub."""
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.STEP_EXECUTE,
        workflow_id="wf-test-103",
        tenant_id="tenant-gamma",
        idempotency_key="op_step_wf-test-103",
    )
    raw_bytes = msg.to_pubsub_json().encode("utf-8")
    rehydrated = WorkflowExecutionMessage.from_pubsub_json(raw_bytes)
    assert rehydrated.workflow_id == "wf-test-103"


# ===========================================================================
# 2. Validation & Poison-Message Rejection Tests
# ===========================================================================

def test_msg_04_unsupported_schema_version_rejected():
    """Unsupported schema version (e.g. 9.9.9) must be rejected."""
    with pytest.raises((MessageValidationError, ValidationError)) as exc:
        WorkflowExecutionMessage(
            schema_version="9.9.9",
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            workflow_id="wf-1",
            tenant_id="t-1",
            idempotency_key="k-1",
        )
    assert "Unsupported schema version" in str(exc.value)


def test_msg_05_malformed_json_rejected():
    """Malformed or invalid JSON strings raise MessageValidationError."""
    with pytest.raises(MessageValidationError) as exc1:
        WorkflowExecutionMessage.from_pubsub_json("{not-valid-json")
    assert "Malformed JSON" in str(exc1.value)

    with pytest.raises(MessageValidationError) as exc2:
        WorkflowExecutionMessage.from_pubsub_json("")
    assert "payload is empty" in str(exc2.value)


def test_msg_06_missing_required_fields_rejected():
    """Payloads missing workflow_id, tenant_id, or idempotency_key are rejected."""
    raw = json.dumps({
        "schema_version": "1.0.0",
        "event_type": "WORKFLOW_DISPATCH",
        "workflow_id": "wf-test-1",
        # missing tenant_id & idempotency_key
    })
    with pytest.raises(MessageValidationError):
        WorkflowExecutionMessage.from_pubsub_json(raw)


def test_msg_07_empty_identifiers_rejected():
    """Empty or whitespace-only strings for key identifiers are rejected."""
    with pytest.raises((MessageValidationError, ValidationError)) as exc:
        WorkflowExecutionMessage(
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            workflow_id="   ",
            tenant_id="tenant-1",
            idempotency_key="op-1",
        )
    assert "workflow_id" in str(exc.value)


def test_msg_08_invalid_expected_version_rejected():
    """Expected version < 1 is rejected."""
    with pytest.raises((MessageValidationError, ValidationError)) as exc:
        WorkflowExecutionMessage(
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            workflow_id="wf-1",
            tenant_id="tenant-1",
            idempotency_key="op-1",
            expected_version=0,
        )
    assert "expected_version" in str(exc.value)


def test_msg_09_future_timestamp_rejected():
    """Message timestamps set in far future (> 10 mins) are rejected."""
    future_time = datetime.now(timezone.utc) + timedelta(hours=2)
    with pytest.raises((MessageValidationError, ValidationError)) as exc:
        WorkflowExecutionMessage(
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            workflow_id="wf-1",
            tenant_id="tenant-1",
            idempotency_key="op-1",
            published_at=future_time,
        )
    assert "in the future" in str(exc.value)


# ===========================================================================
# 3. Publisher Subsystem Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_pub_10_in_memory_publisher(publisher):
    """InMemoryEventPublisher records published messages and returns message_id."""
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id="wf-pub-1",
        tenant_id="tenant-pub",
        idempotency_key="op-pub-1",
    )
    published_id = await publisher.publish_workflow_execution(msg)
    assert published_id == msg.message_id
    assert len(publisher.published_messages) == 1
    assert publisher.published_messages[0].workflow_id == "wf-pub-1"


@pytest.mark.asyncio
async def test_pub_11_publisher_failure_handling(publisher):
    """Publisher raises EventPublishError when transport fails."""
    publisher.set_simulate_failure(True)
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id="wf-pub-2",
        tenant_id="tenant-pub",
        idempotency_key="op-pub-2",
    )
    with pytest.raises(EventPublishError):
        await publisher.publish_workflow_execution(msg)


@pytest.mark.asyncio
async def test_pub_12_concurrent_publishing(publisher):
    """Multiple concurrent async tasks publish safely without data corruption."""
    async def _publish(idx: int):
        msg = WorkflowExecutionMessage(
            event_type=WorkflowEventType.STEP_EXECUTE,
            workflow_id=f"wf-conc-{idx}",
            tenant_id="tenant-conc",
            idempotency_key=f"op-conc-{idx}",
        )
        return await publisher.publish_workflow_execution(msg)

    results = await asyncio.gather(*[_publish(i) for i in range(10)])
    assert len(results) == 10
    assert len(publisher.published_messages) == 10


# ===========================================================================
# 4. Consumer Handler & Invariant Gate Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_con_13_valid_workflow_consumption(store, engine, consumer):
    """Consumer validates message, verifies tenant, acquires claim, and transitions workflow."""
    # 1. Create workflow in CREATED state
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    wf = await engine.create_workflow(
        name="Test Workflow",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
    )
    assert wf["state"] == WorkflowState.CREATED.value
    assert wf["version"] == 1

    # 2. Dispatch message
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    result = await consumer.consume_message(msg)
    assert result["status"] == "PROCESSED"
    assert result["workflow_id"] == wf_id

    # 3. Verify workflow state transitioned to EXECUTING with version increment
    updated_wf = await store.get_workflow(wf_id)
    assert updated_wf["state"] == WorkflowState.EXECUTING.value
    assert updated_wf["version"] == 2


@pytest.mark.asyncio
async def test_con_14_nonexistent_workflow_rejected(consumer):
    """Consumer raises ConsumerExecutionError if target workflow does not exist."""
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id="wf-does-not-exist",
        tenant_id="tenant-acme",
        idempotency_key="op-missing-1",
    )
    with pytest.raises(ConsumerExecutionError) as exc:
        await consumer.consume_message(msg)
    assert "does not exist" in str(exc.value)


@pytest.mark.asyncio
async def test_con_15_tenant_mismatch_rejected(engine, consumer):
    """Consumer strictly rejects cross-tenant messages."""
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    wf = await engine.create_workflow(
        name="Tenant A Workflow",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-alpha",
    )

    # Message specifies tenant-evil instead of tenant-alpha
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-evil",
        idempotency_key=f"op_dispatch_{wf_id}",
    )

    with pytest.raises(ConsumerExecutionError) as exc:
        await consumer.consume_message(msg)
    assert "Tenant mismatch" in str(exc.value)


@pytest.mark.asyncio
async def test_con_16_duplicate_redelivery_replays_cleanly(engine, consumer):
    """Duplicate message redelivery is detected via OperationClaim and dropped cleanly."""
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    wf = await engine.create_workflow(
        name="Deduplication Workflow",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
    )

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_dispatch_{wf_id}",
        expected_version=1,
    )

    # First delivery: PROCESSED
    res1 = await consumer.consume_message(msg)
    assert res1["status"] == "PROCESSED"

    # Second delivery with identical idempotency key: SKIPPED_DUPLICATE
    res2 = await consumer.consume_message(msg)
    assert res2["status"] == "SKIPPED_DUPLICATE"
    assert res2["workflow_id"] == wf_id


@pytest.mark.asyncio
async def test_con_17_terminal_workflow_dropped(store, engine, consumer):
    """Messages targeting COMPLETED or ESCALATED workflows are dropped without error."""
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    wf = await engine.create_workflow(
        name="Terminal Workflow",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
    )
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Running")
    await engine.transition(wf_id, WorkflowState.VERIFYING, detail="Verifying")
    await engine.transition(wf_id, WorkflowState.COMPLETED, detail="Finished")

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.STEP_EXECUTE,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_step_{wf_id}",
        expected_version=4,
    )

    result = await consumer.consume_message(msg)
    assert result["status"] == "SKIPPED_TERMINAL"
    assert result["state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_con_18_occ_version_mismatch_raises_stale_error(engine, consumer):
    """Message specifying stale expected_version triggers StaleWorkflowStateError."""
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    wf = await engine.create_workflow(
        name="OCC Test Workflow",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
    )
    await engine.transition(wf_id, WorkflowState.EXECUTING, detail="Transitioned")

    # Workflow is now at version 2, but event specifies expected_version=1
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.STEP_EXECUTE,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_step_{wf_id}",
        expected_version=1,  # Stale
    )

    with pytest.raises(StaleWorkflowStateError) as exc:
        await consumer.consume_message(msg)
    assert "OCC Mismatch" in str(exc.value)


@pytest.mark.asyncio
async def test_con_19_consume_raw_json_byte_stream(store, engine, consumer):
    """consume_raw_message validates JSON string, deserializes, and executes."""
    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    wf = await engine.create_workflow(
        name="Raw JSON Test Workflow",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
    )

    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-acme",
        idempotency_key=f"op_raw_{wf_id}",
        expected_version=1,
    )

    raw_json = msg.to_pubsub_json()
    res = await consumer.consume_raw_message(raw_json)
    assert res["status"] == "PROCESSED"
    assert res["workflow_id"] == wf_id


@pytest.mark.asyncio
async def test_con_20_correlation_id_propagated_to_contextvars(engine, consumer):
    """Verifies that consuming an event sets correlation contextvars for structured logging."""
    from backend.observability.logging import current_request_id, current_workflow_id, current_tenant_id

    wf_id = str(uuid.uuid4())
    contract = create_acme_contract(wf_id)
    await engine.create_workflow(
        name="Correlation Test",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id=wf_id,
        tenant_id="tenant-trace-1",
    )

    corr_id = "trace-req-8888"
    msg = WorkflowExecutionMessage(
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        workflow_id=wf_id,
        tenant_id="tenant-trace-1",
        correlation_id=corr_id,
        idempotency_key=f"op_trace_{wf_id}",
        expected_version=1,
    )

    await consumer.consume_message(msg)
    assert current_request_id.get() == corr_id
    assert current_workflow_id.get() == wf_id
    assert current_tenant_id.get() == "tenant-trace-1"
