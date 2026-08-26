"""
Application Lifecycle & Graceful Shutdown Subsystem.

Manages FastAPI startup/shutdown lifecycle, tracks active background tasks,
and prevents new agent work from being scheduled once shutdown begins.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI

from backend.observability.logging import setup_logging

logger = logging.getLogger("recoveryos.lifecycle")


class ShutdownManager:
    """
    Coordinates graceful shutdown and bounded task draining.
    """

    def __init__(self):
        self.is_shutting_down: bool = False
        self._active_tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    def register_task(self, task: asyncio.Task) -> None:
        """Register an active background task for lifecycle tracking."""
        if self.is_shutting_down:
            logger.warning("Attempted to register background task while shutting down; cancelling task.")
            task.cancel()
            return

        self._active_tasks.add(task)
        task.add_done_callback(lambda t: self._active_tasks.discard(t))

    def begin_shutdown(self) -> None:
        """Signal the application to reject new operations."""
        self.is_shutting_down = True
        logger.info("[LIFECYCLE] Shutdown initiated. Rejecting new workflow tasks.")

    async def drain_tasks(self, timeout: float = 5.0) -> int:
        """
        Wait for active tasks to complete up to timeout seconds.
        Returns the number of tasks that completed cleanly.
        """
        if not self._active_tasks:
            return 0

        pending = list(self._active_tasks)
        logger.info(f"[LIFECYCLE] Draining {len(pending)} active background tasks (timeout={timeout}s)...")
        
        try:
            done, not_done = await asyncio.wait(pending, timeout=timeout)
            logger.info(f"[LIFECYCLE] Drained {len(done)} tasks. {len(not_done)} tasks remaining.")
            for t in not_done:
                t.cancel()
            return len(done)
        except Exception as e:
            logger.error(f"[LIFECYCLE] Error during task drain: {e}")
            return 0


shutdown_manager = ShutdownManager()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    FastAPI Lifespan Context Manager.
    Initializes structured logging and gracefully drains tasks on shutdown.
    """
    # 1. Startup phase
    setup_logging()
    logger.info("[LIFECYCLE] RecoveryOS starting up...")

    yield

    # 2. Shutdown phase
    logger.info("[LIFECYCLE] RecoveryOS beginning graceful shutdown...")
    shutdown_manager.begin_shutdown()
    await shutdown_manager.drain_tasks(timeout=5.0)
    logger.info("[LIFECYCLE] RecoveryOS shutdown complete.")
