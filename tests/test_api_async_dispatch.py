"""
Tests for API Asynchronous Workflow Dispatch via Event Publisher (Phase 6.3).

Verifies:
1. Scenario launch in Pub/Sub mode returns HTTP 202 and publishes WorkflowExecutionMessage.
2. Scenario launch in in_memory mode preserves legacy local execution.
3. Approval/resume in Pub/Sub mode publishes APPROVAL_RESUME message with correct OCC version.
4. Publisher failure raises clean HTTP 503 and does not falsely claim dispatch.
5. Message contracts pass full Pydantic model validation.
6. GooglePubSubPublisher topic configuration defaults to recoveryos-workflow-execution.
"""

import asyncio
import dataclasses
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.server import app, set_event_publisher, store
from backend.config import config
from backend.events.message_models import (
    WorkflowEventType,
    WorkflowExecutionMessage,
)
from backend.events.publisher import (
    BaseEventPublisher,
    EventPublishError,
    GooglePubSubPublisher,
    InMemoryEventPublisher,
    create_event_publisher,
)
from backend.models.workflow import WorkflowState
from backend.security.tokens import create_access_token


class MockAsyncPublisher(BaseEventPublisher):
    """Test publisher recording published messages."""

    def __init__(self, should_fail: bool = False):
        self.published_messages: list[WorkflowExecutionMessage] = []
        self.should_fail = should_fail

    async def publish_workflow_execution(self, message: WorkflowExecutionMessage) -> str:
        if self.should_fail:
            raise EventPublishError("Simulated Pub/Sub network timeout")
        self.published_messages.append(message)
        return f"pubsub-msg-{len(self.published_messages)}"


@pytest.fixture
def operator_token():
    return create_access_token(
        user_id="op-test-1", role="operator", tenant_id="tenant-acme"
    )


@pytest.fixture
def approver_token():
    return create_access_token(
        user_id="appr-test-1", role="approver", tenant_id="tenant-acme"
    )


@pytest.mark.asyncio
async def test_01_scenario_launch_pubsub_mode_returns_202_and_dispatches(operator_token):
    """In Pub/Sub mode, launch_scenario must construct message, publish, and return HTTP 202."""
    mock_pub = MockAsyncPublisher()
    set_event_publisher(mock_pub)

    custom_cfg = dataclasses.replace(config, event_publisher_backend="pubsub")
    with patch("backend.api.server.config", custom_cfg):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {operator_token}"}
            response = await client.post("/api/scenarios/billing_unavailable", headers=headers)

            assert response.status_code == 202
            body = response.json()
            assert body["status"] == "dispatched"
            assert "workflow_id" in body
            assert body["tenant_id"] == "tenant-acme"
            assert body["pubsub_message_id"] == "pubsub-msg-1"

            # Verify published message contract
            assert len(mock_pub.published_messages) == 1
            msg = mock_pub.published_messages[0]
            assert msg.event_type == WorkflowEventType.WORKFLOW_DISPATCH
            assert msg.workflow_id == body["workflow_id"]
            assert msg.tenant_id == "tenant-acme"
            assert msg.expected_version == 1
            assert msg.idempotency_key == f"op_dispatch_{body['workflow_id']}_v1"
            assert msg.producer_id == "recoveryos-api"
            assert msg.payload.get("scenario") == "billing_unavailable"

            # Verify workflow in store remains in initial CREATED state
            wf = await store.get_workflow(body["workflow_id"])
            assert wf is not None
            assert wf["state"] == "CREATED"
            assert wf["version"] == 1


@pytest.mark.asyncio
async def test_02_scenario_launch_in_memory_mode_preserves_local_execution(operator_token):
    """In in_memory mode, launch_scenario retains HTTP 200 launched response."""
    custom_cfg = dataclasses.replace(config, event_publisher_backend="in_memory")
    with patch("backend.api.server.config", custom_cfg):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {operator_token}"}
            response = await client.post("/api/scenarios/billing_unavailable", headers=headers)

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "launched"
            assert "workflow_id" in body


@pytest.mark.asyncio
async def test_03_approval_resume_pubsub_mode_publishes_event(approver_token):
    """In Pub/Sub mode, approving a workflow publishes APPROVAL_RESUME with current OCC version."""
    mock_pub = MockAsyncPublisher()
    set_event_publisher(mock_pub)

    # Seed workflow awaiting approval at version 3
    wf_id = f"wf-appr-{uuid.uuid4().hex[:6]}"
    appr_id = f"appr-{uuid.uuid4().hex[:6]}"
    await store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": WorkflowState.AWAITING_APPROVAL.value,
        "version": 3,
    })
    await store.save_approval(wf_id, {
        "approval_id": appr_id,
        "workflow_id": wf_id,
        "status": "PENDING",
        "action": "execute_fix",
    })

    custom_cfg = dataclasses.replace(config, event_publisher_backend="pubsub")
    with patch("backend.api.server.config", custom_cfg):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {approver_token}"}
            response = await client.post(
                f"/api/workflows/{wf_id}/approve/{appr_id}",
                json={"approved": True, "reason": "Authorized fix"},
                headers=headers,
            )

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "decided"
            assert body["approved"] is True
            assert body.get("dispatched") is True

            # Verify published message contract
            assert len(mock_pub.published_messages) == 1
            msg = mock_pub.published_messages[0]
            assert msg.event_type == WorkflowEventType.APPROVAL_RESUME
            assert msg.workflow_id == wf_id
            assert msg.tenant_id == "tenant-acme"
            assert msg.expected_version == 3
            assert msg.idempotency_key == f"op_approve_{wf_id}_{appr_id}_v3"
            assert msg.payload.get("approved") is True


@pytest.mark.asyncio
async def test_04_publish_failure_returns_503(operator_token):
    """If publisher fails, API must return HTTP 503 and not silently claim success."""
    failing_pub = MockAsyncPublisher(should_fail=True)
    set_event_publisher(failing_pub)

    custom_cfg = dataclasses.replace(config, event_publisher_backend="pubsub")
    with patch("backend.api.server.config", custom_cfg):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {operator_token}"}
            response = await client.post("/api/scenarios/billing_unavailable", headers=headers)

            assert response.status_code == 503
            assert "Failed to dispatch workflow execution" in response.json()["detail"]


def test_05_publisher_topic_configuration():
    """GooglePubSubPublisher defaults to configured recoveryos-workflow-execution topic."""
    custom_cfg = dataclasses.replace(config, pubsub_topic_workflow_execution="recoveryos-workflow-execution")
    with patch("backend.events.publisher.config", custom_cfg):
        pub = GooglePubSubPublisher(project_id="test-proj")
        assert pub._topic_name == "recoveryos-workflow-execution"
        assert pub._project_id == "test-proj"


def test_06_create_event_publisher_factory():
    """create_event_publisher selects correct backend."""
    pub_inmem = create_event_publisher("in_memory")
    assert isinstance(pub_inmem, InMemoryEventPublisher)

    pub_ps = create_event_publisher("pubsub", project_id="test-proj")
    assert isinstance(pub_ps, GooglePubSubPublisher)
    assert pub_ps._topic_name == config.pubsub_topic_workflow_execution
