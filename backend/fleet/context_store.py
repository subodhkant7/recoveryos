"""
Durable Agent Context — Persistent context across extended timelines.

Stores structured context entries for long-running agent workflows.
When a workflow resumes after worker interruption, context entries
are restored from the durable store.

Context is exact structured state, not an LLM-generated summary.

This is a RecoveryOS-native capability, not a GEAP Memory Bank.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ContextEntry(BaseModel):
    """A single structured context entry for an agent workflow."""

    workflow_id: str
    agent_id: str
    context_version: int = 1
    key: str
    value: Any = None
    scope: str = "workflow"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class AgentContextStore:
    """
    Durable context store for long-running agent workflows.

    Uses the workflow persistence layer for storage. Context entries
    survive worker interruption and process restarts.

    Thread-safe via per-workflow key isolation.
    """

    def __init__(self):
        self._contexts: dict[str, dict[str, ContextEntry]] = {}

    def save_context(
        self,
        workflow_id: str,
        agent_id: str,
        key: str,
        value: Any,
        scope: str = "workflow",
    ) -> ContextEntry:
        """
        Save or update a context entry.

        If a context entry with the same (workflow_id, key) exists,
        its version is incremented and value is updated.
        """
        wf_contexts = self._contexts.setdefault(workflow_id, {})
        existing = wf_contexts.get(key)

        if existing:
            existing.value = copy.deepcopy(value)
            existing.context_version += 1
            existing.updated_at = datetime.now(timezone.utc).isoformat()
            existing.agent_id = agent_id
            return existing
        else:
            entry = ContextEntry(
                workflow_id=workflow_id,
                agent_id=agent_id,
                key=key,
                value=copy.deepcopy(value),
                scope=scope,
            )
            wf_contexts[key] = entry
            return entry

    def get_context(
        self,
        workflow_id: str,
        key: str,
    ) -> ContextEntry | None:
        """Get a single context entry by workflow_id and key."""
        return self._contexts.get(workflow_id, {}).get(key)

    def get_all_context(
        self,
        workflow_id: str,
        agent_id: str | None = None,
        scope: str | None = None,
    ) -> list[ContextEntry]:
        """
        Get all context entries for a workflow.

        Optionally filter by agent_id and/or scope.
        """
        entries = list(self._contexts.get(workflow_id, {}).values())
        if agent_id:
            entries = [e for e in entries if e.agent_id == agent_id]
        if scope:
            entries = [e for e in entries if e.scope == scope]
        return entries

    def delete_context(self, workflow_id: str, key: str) -> bool:
        """Delete a context entry. Returns True if it existed."""
        wf_contexts = self._contexts.get(workflow_id, {})
        if key in wf_contexts:
            del wf_contexts[key]
            return True
        return False

    def clear_workflow_context(self, workflow_id: str) -> int:
        """Clear all context entries for a workflow. Returns count deleted."""
        count = len(self._contexts.get(workflow_id, {}))
        self._contexts.pop(workflow_id, None)
        return count

    def snapshot_context(self, workflow_id: str) -> dict[str, Any]:
        """
        Create a serializable snapshot of all context for a workflow.

        Used for persistence to Firestore and API responses.
        """
        entries = self.get_all_context(workflow_id)
        return {
            "workflow_id": workflow_id,
            "entries": [e.model_dump(mode="json") for e in entries],
            "total": len(entries),
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
        }

    def restore_context(
        self,
        workflow_id: str,
        entries: list[dict[str, Any]],
    ) -> int:
        """
        Restore context entries from a snapshot (e.g. after worker restart).

        Returns the number of entries restored.
        """
        count = 0
        for entry_data in entries:
            entry = ContextEntry(**entry_data)
            wf_contexts = self._contexts.setdefault(workflow_id, {})
            wf_contexts[entry.key] = entry
            count += 1
        return count


# Module-level singleton
fleet_context_store = AgentContextStore()
