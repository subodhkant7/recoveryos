"""
In-memory real-time event broadcast bus for RecoveryOS.

Allows SSE endpoints and live subscribers to receive workflow events
immediately when published without continuous database polling loops.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("recoveryos.events.broadcast")


class EventBroadcaster:
    """
    Thread-safe, async in-memory pub/sub event broadcaster for workflows.
    """

    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, workflow_id: str) -> asyncio.Queue:
        """Subscribe to live events for a specific workflow."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            if workflow_id not in self._subscribers:
                self._subscribers[workflow_id] = set()
            self._subscribers[workflow_id].add(queue)
        return queue

    async def unsubscribe(self, workflow_id: str, queue: asyncio.Queue) -> None:
        """Unsubscribe and cleanup the queue."""
        async with self._lock:
            if workflow_id in self._subscribers:
                self._subscribers[workflow_id].discard(queue)
                if not self._subscribers[workflow_id]:
                    del self._subscribers[workflow_id]

    async def broadcast(self, workflow_id: str, event_data: dict[str, Any]) -> None:
        """Broadcast an event to all active subscribers for the workflow."""
        async with self._lock:
            queues = list(self._subscribers.get(workflow_id, set()))

        for q in queues:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(event_data)
            except Exception as e:
                logger.debug(f"Failed to push event to subscriber queue: {e}")


# Singleton instance
event_broadcaster = EventBroadcaster()
