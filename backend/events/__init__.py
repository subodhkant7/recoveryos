"""
Asynchronous Event Subsystem for RecoveryOS.

Provides message contracts, publishers, and consumer boundaries for distributed workflow execution.
"""

from backend.events.message_models import (
    WorkflowExecutionMessage,
    WorkflowEventType,
    MessageValidationError,
    SUPPORTED_SCHEMA_VERSIONS,
)
from backend.events.publisher import (
    BaseEventPublisher,
    InMemoryEventPublisher,
    GooglePubSubPublisher,
    EventPublishError,
    create_event_publisher,
)
from backend.events.consumer import (
    WorkflowEventConsumer,
    ConsumerExecutionError,
)

__all__ = [
    "WorkflowExecutionMessage",
    "WorkflowEventType",
    "MessageValidationError",
    "SUPPORTED_SCHEMA_VERSIONS",
    "BaseEventPublisher",
    "InMemoryEventPublisher",
    "GooglePubSubPublisher",
    "EventPublishError",
    "create_event_publisher",
    "WorkflowEventConsumer",
    "ConsumerExecutionError",
]
