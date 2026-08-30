"""
Tests for Durable Agent Context — persistent context across extended timelines.
"""

import pytest

from backend.fleet.context_store import AgentContextStore, ContextEntry


class TestAgentContextStore:
    """Test durable agent context store."""

    def setup_method(self):
        """Fresh context store for each test."""
        self.store = AgentContextStore()

    def test_save_and_get_context(self):
        """Save and retrieve a context entry."""
        entry = self.store.save_context(
            workflow_id="wf-1",
            agent_id="billing-agent",
            key="billing_provider",
            value={"provider": "stripe", "status": "failed"},
            scope="billing",
        )
        assert entry.workflow_id == "wf-1"
        assert entry.key == "billing_provider"
        assert entry.value["provider"] == "stripe"
        assert entry.context_version == 1

        retrieved = self.store.get_context("wf-1", "billing_provider")
        assert retrieved is not None
        assert retrieved.value == entry.value

    def test_update_increments_version(self):
        """Updating a context entry increments the version."""
        self.store.save_context("wf-1", "billing-agent", "provider", "stripe")
        self.store.save_context("wf-1", "billing-agent", "provider", "paypal")

        entry = self.store.get_context("wf-1", "provider")
        assert entry.context_version == 2
        assert entry.value == "paypal"

    def test_get_all_context(self):
        """Get all context entries for a workflow."""
        self.store.save_context("wf-1", "billing-agent", "provider", "stripe", "billing")
        self.store.save_context("wf-1", "risk-agent", "risk_score", 42, "risk")
        self.store.save_context("wf-1", "billing-agent", "plan_tier", "enterprise", "billing")

        entries = self.store.get_all_context("wf-1")
        assert len(entries) == 3

    def test_filter_by_agent(self):
        """Filter context entries by agent_id."""
        self.store.save_context("wf-1", "billing-agent", "provider", "stripe")
        self.store.save_context("wf-1", "risk-agent", "risk_score", 42)

        billing_entries = self.store.get_all_context("wf-1", agent_id="billing-agent")
        assert len(billing_entries) == 1
        assert billing_entries[0].agent_id == "billing-agent"

    def test_filter_by_scope(self):
        """Filter context entries by scope."""
        self.store.save_context("wf-1", "billing-agent", "provider", "stripe", "billing")
        self.store.save_context("wf-1", "risk-agent", "risk_score", 42, "risk")

        billing_entries = self.store.get_all_context("wf-1", scope="billing")
        assert len(billing_entries) == 1

    def test_delete_context(self):
        """Delete a context entry."""
        self.store.save_context("wf-1", "billing-agent", "provider", "stripe")
        assert self.store.delete_context("wf-1", "provider") is True
        assert self.store.get_context("wf-1", "provider") is None

    def test_delete_nonexistent_returns_false(self):
        """Deleting nonexistent key returns False."""
        assert self.store.delete_context("wf-1", "nonexistent") is False

    def test_clear_workflow_context(self):
        """Clear all context for a workflow."""
        self.store.save_context("wf-1", "billing-agent", "a", 1)
        self.store.save_context("wf-1", "billing-agent", "b", 2)
        count = self.store.clear_workflow_context("wf-1")
        assert count == 2
        assert self.store.get_all_context("wf-1") == []

    def test_context_survives_simulated_interruption(self):
        """Context entries survive simulated worker interruption via snapshot/restore."""
        # Phase 1: Save context
        self.store.save_context("wf-1", "billing-agent", "provider", "stripe", "billing")
        self.store.save_context("wf-1", "billing-agent", "attempt", 1, "recovery")

        # Phase 2: Take snapshot (simulates durable persistence)
        snapshot = self.store.snapshot_context("wf-1")
        assert snapshot["total"] == 2

        # Phase 3: Simulate interruption — create new store
        new_store = AgentContextStore()
        assert new_store.get_all_context("wf-1") == []

        # Phase 4: Restore from snapshot
        restored = new_store.restore_context("wf-1", snapshot["entries"])
        assert restored == 2

        # Phase 5: Verify restored context
        provider = new_store.get_context("wf-1", "provider")
        assert provider is not None
        assert provider.value == "stripe"
        assert provider.scope == "billing"

        attempt = new_store.get_context("wf-1", "attempt")
        assert attempt is not None
        assert attempt.value == 1

    def test_workflow_isolation(self):
        """Context entries for different workflows are isolated."""
        self.store.save_context("wf-1", "billing-agent", "provider", "stripe")
        self.store.save_context("wf-2", "billing-agent", "provider", "paypal")

        wf1 = self.store.get_context("wf-1", "provider")
        wf2 = self.store.get_context("wf-2", "provider")
        assert wf1.value == "stripe"
        assert wf2.value == "paypal"

    def test_snapshot_format(self):
        """Snapshot produces serializable format."""
        self.store.save_context("wf-1", "billing-agent", "provider", "stripe")
        snapshot = self.store.snapshot_context("wf-1")

        assert "workflow_id" in snapshot
        assert "entries" in snapshot
        assert "total" in snapshot
        assert "snapshot_at" in snapshot
        assert snapshot["total"] == 1
        assert isinstance(snapshot["entries"][0], dict)

    def test_deep_copy_isolation(self):
        """Context values are deep-copied to prevent aliasing."""
        original = {"nested": {"key": "value"}}
        self.store.save_context("wf-1", "agent", "data", original)

        # Mutate the original
        original["nested"]["key"] = "mutated"

        # Stored value should be unchanged
        entry = self.store.get_context("wf-1", "data")
        assert entry.value["nested"]["key"] == "value"
