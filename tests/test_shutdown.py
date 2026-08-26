"""
Phase 5.4.5: Graceful Shutdown and Lifecycle Test Suite.

Verifies ShutdownManager task tracking, shutdown state enforcement, and bounded draining.
"""

import asyncio
import pytest
from backend.lifecycle import ShutdownManager


@pytest.mark.asyncio
async def test_shutdown_manager_task_registration_and_cleanup():
    """Verify background tasks are registered and cleanly removed when complete."""
    mgr = ShutdownManager()

    async def _dummy_task():
        await asyncio.sleep(0.02)

    task = asyncio.create_task(_dummy_task())
    mgr.register_task(task)

    assert len(mgr._active_tasks) == 1
    await task
    # Done callback should discard task
    assert len(mgr._active_tasks) == 0


@pytest.mark.asyncio
async def test_shutdown_manager_rejects_new_tasks_when_shutting_down():
    """Verify new tasks registered during shutdown are cancelled immediately."""
    mgr = ShutdownManager()
    mgr.begin_shutdown()
    assert mgr.is_shutting_down is True

    executed = False

    async def _rejected_task():
        nonlocal executed
        executed = True

    task = asyncio.create_task(_rejected_task())
    mgr.register_task(task)

    try:
        await task
    except asyncio.CancelledError:
        pass

    assert task.cancelled()
    assert executed is False


@pytest.mark.asyncio
async def test_shutdown_manager_drain_tasks():
    """Verify active tasks are given time to complete up to timeout."""
    mgr = ShutdownManager()

    async def _quick_task():
        await asyncio.sleep(0.01)

    t1 = asyncio.create_task(_quick_task())
    t2 = asyncio.create_task(_quick_task())
    mgr.register_task(t1)
    mgr.register_task(t2)

    mgr.begin_shutdown()
    drained_count = await mgr.drain_tasks(timeout=0.1)
    assert drained_count == 2
    assert len(mgr._active_tasks) == 0
