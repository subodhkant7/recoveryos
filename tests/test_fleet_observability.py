"""
Tests for Fleet Observability — OpenTelemetry-compatible structured audit traces.
"""

import pytest

from backend.fleet.observability import (
    FleetTraceEvent,
    FleetTracer,
    current_trace_id,
    generate_span_id,
    generate_trace_id,
)


class TestFleetObservability:
    """Test fleet trace collection and propagation."""

    def setup_method(self):
        """Fresh tracer for each test."""
        self.tracer = FleetTracer()

    def test_generate_trace_id_format(self):
        """Trace ID is 32 hex digits (W3C compatible)."""
        trace_id = generate_trace_id()
        assert len(trace_id) == 32
        assert all(c in "0123456789abcdef" for c in trace_id)

    def test_generate_span_id_format(self):
        """Span ID is 16 hex digits (W3C compatible)."""
        span_id = generate_span_id()
        assert len(span_id) == 16
        assert all(c in "0123456789abcdef" for c in span_id)

    def test_trace_id_uniqueness(self):
        """Generated trace IDs are unique."""
        ids = [generate_trace_id() for _ in range(100)]
        assert len(set(ids)) == 100

    def test_record_event(self):
        """Record a fleet trace event."""
        event = self.tracer.record_event(
            workflow_id="wf-1",
            agent_id="billing-agent",
            event_type="TOOL_INVOCATION",
            tool="setup_billing",
            outcome="SUCCESS",
            parent_agent_id="orchestrator",
        )
        assert event.agent_id == "billing-agent"
        assert event.tool == "setup_billing"
        assert event.parent_agent_id == "orchestrator"
        assert event.trace_id
        assert event.span_id

    def test_trace_ids_propagate(self):
        """Trace IDs propagate across events in the same workflow."""
        trace_id = self.tracer.start_trace("wf-1")

        e1 = self.tracer.record_event(
            workflow_id="wf-1",
            agent_id="orchestrator",
            event_type="DELEGATION",
        )
        e2 = self.tracer.record_event(
            workflow_id="wf-1",
            agent_id="billing-agent",
            event_type="TOOL_INVOCATION",
            parent_span_id=e1.span_id,
        )

        assert e1.trace_id == trace_id
        assert e2.trace_id == trace_id
        assert e2.parent_span_id == e1.span_id

    def test_get_trace_events(self):
        """Get all trace events for a workflow."""
        self.tracer.record_event("wf-1", "billing-agent", "TOOL_INVOCATION")
        self.tracer.record_event("wf-1", "verification-agent", "VERIFICATION")

        events = self.tracer.get_trace("wf-1")
        assert len(events) == 2

    def test_trace_summary(self):
        """Get trace summary with aggregate info."""
        self.tracer.record_event(
            "wf-1", "billing-agent", "GATEWAY_DECISION", tool="setup_billing"
        )
        self.tracer.record_event(
            "wf-1", "verification-agent", "VERIFICATION", tool="verify_outcome"
        )

        summary = self.tracer.get_trace_summary("wf-1")
        assert summary["total"] == 2
        assert "billing-agent" in summary["agents_involved"]
        assert "verification-agent" in summary["agents_involved"]
        assert summary["gateway_decisions"] == 1
        assert "setup_billing" in summary["tools_invoked"]

    def test_empty_trace_summary(self):
        """Empty trace returns valid empty summary."""
        summary = self.tracer.get_trace_summary("wf-nonexistent")
        assert summary["total"] == 0
        assert summary["events"] == []

    def test_workflow_isolation(self):
        """Trace events for different workflows are isolated."""
        self.tracer.record_event("wf-1", "billing-agent", "TOOL_INVOCATION")
        self.tracer.record_event("wf-2", "risk-agent", "TOOL_INVOCATION")

        wf1_events = self.tracer.get_trace("wf-1")
        wf2_events = self.tracer.get_trace("wf-2")
        assert len(wf1_events) == 1
        assert len(wf2_events) == 1
        assert wf1_events[0].agent_id == "billing-agent"
        assert wf2_events[0].agent_id == "risk-agent"

    def test_clear_trace(self):
        """Clear trace events for a workflow."""
        self.tracer.record_event("wf-1", "billing-agent", "TOOL_INVOCATION")
        self.tracer.clear_trace("wf-1")
        assert self.tracer.get_trace("wf-1") == []

    def test_event_serialization(self):
        """Fleet trace event serializes to dict correctly."""
        event = FleetTraceEvent(
            agent_id="billing-agent",
            event_type="TOOL_INVOCATION",
            tool="setup_billing",
        )
        data = event.model_dump(mode="json")
        assert isinstance(data, dict)
        assert data["agent_id"] == "billing-agent"
        assert "trace_id" in data
        assert "span_id" in data

    def test_metadata_field(self):
        """Trace events can carry arbitrary metadata."""
        event = self.tracer.record_event(
            workflow_id="wf-1",
            agent_id="billing-agent",
            event_type="TOOL_INVOCATION",
            metadata={"provider": "stripe", "plan_tier": "enterprise"},
        )
        assert event.metadata["provider"] == "stripe"
