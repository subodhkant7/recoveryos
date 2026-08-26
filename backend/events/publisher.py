"""
Publisher subsystem for asynchronous workflow execution events.

Provides abstract interface, in-memory publisher for testing/local execution,
and Google Cloud Pub/Sub publisher boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

from backend.config import config
from backend.events.message_models import WorkflowExecutionMessage


logger = logging.getLogger("recoveryos.events.publisher")


class EventPublishError(Exception):
    """Raised when an event fails to publish to the message transport."""
    pass


class BaseEventPublisher(ABC):
    """Abstract base publisher interface for workflow execution events."""

    @abstractmethod
    async def publish_workflow_execution(self, message: WorkflowExecutionMessage) -> str:
        """
        Publish a WorkflowExecutionMessage to the transport.
        Returns the transport-assigned message ID or tracking ID.
        """
        pass


class InMemoryEventPublisher(BaseEventPublisher):
    """
    In-memory event publisher for unit tests, local verification,
    and single-node deterministic testing.
    """

    def __init__(self, simulate_failure: bool = False):
        self._published_messages: list[WorkflowExecutionMessage] = []
        self._simulate_failure = simulate_failure
        self._lock = asyncio.Lock()

    @property
    def published_messages(self) -> list[WorkflowExecutionMessage]:
        return list(self._published_messages)

    def set_simulate_failure(self, fail: bool) -> None:
        self._simulate_failure = fail

    def clear(self) -> None:
        self._published_messages.clear()

    async def publish_workflow_execution(self, message: WorkflowExecutionMessage) -> str:
        if self._simulate_failure:
            raise EventPublishError("Simulated event publisher failure")

        async with self._lock:
            self._published_messages.append(message)
            logger.info(
                "In-memory event published",
                extra={
                    "event_name": "EVENT_PUBLISHED_IN_MEMORY",
                    "workflow_id": message.workflow_id,
                    "tenant_id": message.tenant_id,
                    "message_id": message.message_id,
                    "event_type": message.event_type.value,
                },
            )
            return message.message_id


class GooglePubSubPublisher(BaseEventPublisher):
    """
    Google Cloud Pub/Sub publisher boundary for asynchronous message dispatch.
    """

    def __init__(
        self,
        project_id: str | None = None,
        topic_name: str | None = None,
    ):
        self._project_id = project_id or config.google_cloud_project
        self._topic_name = topic_name or config.pubsub_topic_workflow_execution
        self._client = None
        self._topic_path = None

    def _get_client_and_path(self):
        if self._client is None:
            from google.cloud import pubsub_v1

            self._client = pubsub_v1.PublisherClient()
            self._topic_path = self._client.topic_path(self._project_id, self._topic_name)
        return self._client, self._topic_path

    async def publish_workflow_execution(self, message: WorkflowExecutionMessage) -> str:
        client, topic_path = self._get_client_and_path()
        data = message.to_pubsub_json().encode("utf-8")
        attributes = message.to_pubsub_attributes()

        loop = asyncio.get_running_loop()

        def _sync_publish():
            future = client.publish(topic_path, data, **attributes)
            return future.result(timeout=10.0)

        try:
            pubsub_id = await loop.run_in_executor(None, _sync_publish)
            logger.info(
                "Event published to Google Cloud Pub/Sub",
                extra={
                    "event_name": "EVENT_PUBLISHED_PUBSUB",
                    "workflow_id": message.workflow_id,
                    "tenant_id": message.tenant_id,
                    "message_id": message.message_id,
                    "pubsub_id": pubsub_id,
                    "event_type": message.event_type.value,
                },
            )
            return str(pubsub_id)
        except Exception as e:
            logger.error(
                "Failed to publish event to Google Cloud Pub/Sub",
                extra={
                    "event_name": "EVENT_PUBLISH_FAILED",
                    "workflow_id": message.workflow_id,
                    "tenant_id": message.tenant_id,
                    "message_id": message.message_id,
                    "error": str(e),
                },
            )
            raise EventPublishError(f"Google Cloud Pub/Sub publish failed: {e}") from e


def create_event_publisher(
    backend: str | None = None,
    project_id: str | None = None,
    topic_name: str | None = None,
) -> BaseEventPublisher:
    """Factory creating the appropriate event publisher based on configuration."""
    selected = backend or config.event_publisher_backend
    if selected == "pubsub":
        return GooglePubSubPublisher(project_id=project_id, topic_name=topic_name)
    return InMemoryEventPublisher()
