"""
Fleet Observability — OpenTelemetry-compatible structured audit traces.

Extends the existing WorkflowEvent model with fleet-level trace fields:
trace_id, span_id, parent_span_id, agent_id, parent_agent_id.

Follows W3C Trace Context conventions but does not include an actual
OpenTelemetry SDK exporter. Described honestly as:

    "OpenTelemetry-compatible structured audit traces"

This is a RecoveryOS-native capability.
"""

from __future__ import annotations

import contextvars
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


# Fleet-level context variables (extend existing observability contextvars)
current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_trace_id", default=""
)
current_agent_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_agent_id", default=""
)
current_span_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_span_id", default=""
)


def generate_trace_id() -> str:
    """Generate a W3C-compatible 32-hex-digit trace ID."""
    return uuid.uuid4().hex


def generate_span_id() -> str:
    """Generate a W3C-compatible 16-hex-digit span ID."""
    return uuid.uuid4().hex[:16]


class FleetTraceEvent(BaseModel):
    """
    An audit trace event following OpenTelemetry-compatible structure.

    Contains the standard trace context fields (trace_id, span_id,
    parent_span_id) plus fleet-specific agent identification.
    """

    trace_id: str = Field(default_factory=generate_trace_id)
    span_id: str = Field(default_factory=generate_span_id)
    parent_span_id: str = ""
    agent_id: str = ""
    parent_agent_id: str = ""
    workflow_id: str = ""
    event_type: str = ""
    tool: str = ""
    decision: str = ""
    outcome: str = ""
    status: str = ""
    detail: str = ""
    correlation_id: str = ""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    duration_ms: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FleetTracer:
    """
    Fleet-level trace collector.

    Collects structured trace events for a workflow's agent lifecycle.
    Events are stored in memory and can be retrieved for API responses
    and UI rendering.
    """

    def __init__(self):
        self._traces: dict[str, list[FleetTraceEvent]] = {}

    def start_trace(self, workflow_id: str) -> str:
        """Start a new trace for a workflow. Returns the trace_id."""
        trace_id = generate_trace_id()
        self._traces.setdefault(workflow_id, [])
        current_trace_id.set(trace_id)
        return trace_id

    def record_event(
        self,
        workflow_id: str,
        agent_id: str,
        event_type: str,
        tool: str = "",
        decision: str = "",
        outcome: str = "",
        status: str = "",
        detail: str = "",
        parent_agent_id: str = "",
        parent_span_id: str = "",
        correlation_id: str = "",
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> FleetTraceEvent:
        """Record a fleet trace event."""
        trace_id = current_trace_id.get("") or generate_trace_id()
        span_id = generate_span_id()

        event = FleetTraceEvent(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            workflow_id=workflow_id,
            event_type=event_type,
            tool=tool,
            decision=decision,
            outcome=outcome,
            status=status,
            detail=detail,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )

        self._traces.setdefault(workflow_id, []).append(event)
        return event

    def get_trace(self, workflow_id: str) -> list[FleetTraceEvent]:
        """Get all trace events for a workflow."""
        return list(self._traces.get(workflow_id, []))

    def get_trace_summary(self, workflow_id: str) -> dict[str, Any]:
        """Get a summary of the trace for a workflow."""
        events = self.get_trace(workflow_id)
        if not events:
            return {
                "workflow_id": workflow_id,
                "trace_id": "",
                "events": [],
                "total": 0,
                "agents_involved": [],
                "gateway_decisions": 0,
                "tools_invoked": [],
            }

        trace_id = events[0].trace_id if events else ""
        agents = list(set(e.agent_id for e in events if e.agent_id))
        gateway_decisions = sum(1 for e in events if e.event_type == "GATEWAY_DECISION")
        tools = list(set(e.tool for e in events if e.tool))

        return {
            "workflow_id": workflow_id,
            "trace_id": trace_id,
            "events": [e.model_dump(mode="json") for e in events],
            "total": len(events),
            "agents_involved": agents,
            "gateway_decisions": gateway_decisions,
            "tools_invoked": tools,
        }

    def clear_trace(self, workflow_id: str) -> None:
        """Clear trace events for a workflow."""
        self._traces.pop(workflow_id, None)


# Module-level singleton
fleet_tracer = FleetTracer()
