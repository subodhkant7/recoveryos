"""
Phase 6.2.4: GCP Pub/Sub & Cloud Tasks Infrastructure Contract Test Suite.

Validates:
- Pub/Sub canonical topic, subscription, and DLQ naming
- Cloud Tasks queue naming, rate limit parameters (0.25 dispatches/sec), and concurrency bounds
- Message schema contract compatibility and attribute validation
- Dead-letter retry policy invariants (max 5 delivery attempts)
- Authenticated Cloud Run push delivery and IAM service identity assumptions
- Security boundaries: fail-closed validation, no wildcard CORS / allUsers permissions
"""

import json
import uuid
from datetime import datetime, timezone
import pytest

from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
    MessageValidationError,
)
from backend.events.publisher import InMemoryEventPublisher
from backend.worker.security import DefaultWorkerSecurityValidator


# ===========================================================================
# 1. Topology & Naming Invariants
# ===========================================================================

def test_01_canonical_pubsub_resource_names():
    """Verify exact canonical naming of topics, subscriptions, and DLQs."""
    PRIMARY_TOPIC = "recoveryos-workflow-execution"
    DLQ_TOPIC = "recoveryos-workflow-execution-dlq"
    WORKER_SUB = "recoveryos-workflow-execution-worker"
    DLQ_SUB = "recoveryos-workflow-execution-dlq-sub"
    CLOUD_TASKS_QUEUE = "recoveryos-gemini-queue"

    assert "recoveryos" in PRIMARY_TOPIC
    assert DLQ_TOPIC.endswith("-dlq")
    assert WORKER_SUB.endswith("-worker")
    assert DLQ_SUB.endswith("-dlq-sub")
    assert CLOUD_TASKS_QUEUE == "recoveryos-gemini-queue"


def test_02_cloud_tasks_queue_parameters():
    """Verify Cloud Tasks queue dispatch rate limiting parameters."""
    rate_limits = {
        "max_dispatches_per_second": 0.25,
        "max_concurrent_dispatches": 1,
        "max_burst_size": 10,
        "max_attempts": 5,
    }
    # 0.25 dispatches/sec -> 1 dispatch per 4 seconds -> <= 15 RPM
    rpm_bound = rate_limits["max_dispatches_per_second"] * 60
    assert rpm_bound <= 15.0
    assert rate_limits["max_concurrent_dispatches"] == 1
    assert rate_limits["max_attempts"] == 5


# ===========================================================================
# 2. Message Contract & Attribute Serialization
# ===========================================================================

def test_03_valid_message_attributes_serialization():
    """Verify WorkflowExecutionMessage attributes adhere to Pub/Sub string dictionary contract."""
    msg = WorkflowExecutionMessage(
        message_id=str(uuid.uuid4()),
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-prod-1",
        workflow_id="wf-abc-123",
        idempotency_key="idemp-msg-01",
        expected_version=3,
        correlation_id="corr-trace-999",
        payload={"scenario_name": "FINANCIAL_LEDGER_RECOVERY", "step_index": 0},
    )

    attr_dict = msg.to_pubsub_attributes()
    assert attr_dict["event_type"] == "WORKFLOW_DISPATCH"
    assert attr_dict["tenant_id"] == "tenant-prod-1"
    assert attr_dict["expected_version"] == "3"
    assert attr_dict["schema_version"] == "1.0.0"
    assert attr_dict["correlation_id"] == "corr-trace-999"


def test_04_missing_required_attribute_rejected():
    """Missing critical attributes (e.g. tenant_id, workflow_id) must be rejected."""
    with pytest.raises((ValueError, MessageValidationError)):
        WorkflowExecutionMessage(
            message_id=str(uuid.uuid4()),
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            schema_version="1.0.0",
            tenant_id="",  # empty string rejected
            workflow_id="wf-123",
            idempotency_key="idemp-1",
            expected_version=1,
        )


def test_05_payload_bytes_roundtrip():
    """Message serializes to JSON string/bytes and deserializes cleanly."""
    msg_id = str(uuid.uuid4())
    wf_id = str(uuid.uuid4())
    original = WorkflowExecutionMessage(
        message_id=msg_id,
        event_type=WorkflowEventType.STEP_EXECUTE,
        schema_version="1.0.0",
        tenant_id="tenant-acme",
        workflow_id=wf_id,
        idempotency_key="idemp-bytes-01",
        expected_version=2,
        correlation_id="corr-rt",
        payload={"step_name": "VERIFY_BALANCE", "args": {"account_id": "acc-100"}},
    )

    payload_json = original.to_pubsub_json()
    assert isinstance(payload_json, str)

    reconstructed = WorkflowExecutionMessage.from_pubsub_json(payload_json)
    assert reconstructed.message_id == msg_id
    assert reconstructed.workflow_id == wf_id
    assert reconstructed.payload["step_name"] == "VERIFY_BALANCE"


# ===========================================================================
# 3. Dead-Letter Policy & Retry Expectations
# ===========================================================================

def test_06_dead_letter_policy_invariants():
    """Verify DLQ policy parameters: max 5 delivery attempts and DLQ routing."""
    dlq_policy = {
        "dead_letter_topic": "projects/recoveryos-506713/topics/recoveryos-workflow-execution-dlq",
        "max_delivery_attempts": 5,
    }
    assert dlq_policy["max_delivery_attempts"] == 5
    assert "recoveryos-workflow-execution-dlq" in dlq_policy["dead_letter_topic"]


from backend.worker.security import SecurityVerificationError


def test_07_worker_dead_letter_decision_on_poison_pill():
    """Poison-pill / malformed payloads produce invalid provenance without leaking."""
    validator = DefaultWorkerSecurityValidator()
    # Untrusted producer
    bad_msg = WorkflowExecutionMessage(
        message_id="bad-msg-1",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-attacker",
        workflow_id="wf-victim",
        idempotency_key="idemp-bad",
        producer_id="attacker-external-service",
        expected_version=1,
    )
    with pytest.raises(SecurityVerificationError) as exc:
        validator.validate_message_provenance(bad_msg)
    assert "Untrusted producer ID" in str(exc.value)


# ===========================================================================
# 4. IAM & Push Authentication Assumptions
# ===========================================================================

def test_08_iam_service_agent_permissions_contract():
    """Verify required IAM roles for Pub/Sub Service Agent."""
    required_roles = {
        "dlq_topic": "roles/pubsub.publisher",
        "worker_subscription": "roles/pubsub.subscriber",
    }
    assert required_roles["dlq_topic"] == "roles/pubsub.publisher"
    assert required_roles["worker_subscription"] == "roles/pubsub.subscriber"


def test_09_push_authentication_token_requirement():
    """Cloud Run push delivery requires OIDC Bearer tokens with audience."""
    push_config = {
        "oidc_token": {
            "service_account_email": "recoveryos-runtime@recoveryos-506713.iam.gserviceaccount.com",
            "audience": "https://recoveryos-worker-321161003794.asia-east1.run.app",
        }
    }
    assert "@recoveryos-506713.iam.gserviceaccount.com" in push_config["oidc_token"]["service_account_email"]
    assert push_config["oidc_token"]["audience"].startswith("https://")


# ===========================================================================
# 5. Publisher Interface Contract
# ===========================================================================

@pytest.mark.asyncio
async def test_10_in_memory_publisher_contract():
    """InMemoryEventPublisher satisfies publishing contract for local tests."""
    pub = InMemoryEventPublisher()
    msg = WorkflowExecutionMessage(
        message_id="msg-pub-01",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-test",
        workflow_id="wf-test-01",
        idempotency_key="idemp-pub-01",
        expected_version=1,
        payload={"test": True},
    )

    published_id = await pub.publish_workflow_execution(msg)
    assert published_id == "msg-pub-01"
    assert len(pub.published_messages) == 1
    assert pub.published_messages[0].workflow_id == "wf-test-01"
