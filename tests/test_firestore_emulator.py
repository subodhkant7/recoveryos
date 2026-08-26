"""
Firestore Emulator Integration Test Suite.

Validates that FirestoreWorkflowStore operates correctly against a live
Firestore emulator host, testing document hierarchies, subcollections,
store recreation, and transactional OCC.
"""

import os
import uuid
import pytest
from datetime import datetime, timezone

from backend.config import config
from backend.models.workflow import WorkflowState
from backend.persistence.workflow_store import FirestoreWorkflowStore, StaleWorkflowStateError


def is_firestore_emulator_active() -> bool:
    """Check if the Firestore emulator is reachable."""
    host = os.environ.get("FIRESTORE_EMULATOR_HOST") or config.firestore_emulator_host
    if not host:
        return False
    import socket
    try:
        parts = host.split(":")
        ip = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 8080
        with socket.create_connection((ip, port), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not is_firestore_emulator_active(),
    reason="Firestore emulator not active (set FIRESTORE_EMULATOR_HOST to run live integration tests)",
)


@pytest.fixture
def firestore_store():
    return FirestoreWorkflowStore(project_id="recoveryos-test")


@pytest.mark.asyncio
async def test_firestore_workflow_crud_and_occ(firestore_store):
    """Test Firestore workflow CRUD and optimistic concurrency control."""
    workflow_id = str(uuid.uuid4())
    wf_data = {
        "workflow_id": workflow_id,
        "name": "Firestore Integration Workflow",
        "scenario": "test",
        "state": "CREATED",
        "version": 1,
    }
    await firestore_store.save_workflow(wf_data)

    retrieved = await firestore_store.get_workflow(workflow_id)
    assert retrieved is not None
    assert retrieved["name"] == "Firestore Integration Workflow"

    # OCC Update
    retrieved["state"] = "EXECUTING"
    await firestore_store.save_workflow(retrieved, expected_version=1)

    updated = await firestore_store.get_workflow(workflow_id)
    assert updated["state"] == "EXECUTING"
    assert updated["version"] == 2

    # Stale version update must raise StaleWorkflowStateError
    with pytest.raises(StaleWorkflowStateError):
        await firestore_store.save_workflow(updated, expected_version=1)


@pytest.mark.asyncio
async def test_firestore_worker_a_b_concurrency_race(firestore_store):
    """
    Test Worker A vs Worker B concurrency failure sequence:
    Worker A: reads workflow version N
    Worker B: updates workflow to version N+1
    Worker A: attempts stale write
    Expected: Worker A receives StaleWorkflowStateError, version N+1 remains intact.
    """
    workflow_id = str(uuid.uuid4())
    wf_data = {
        "workflow_id": workflow_id,
        "name": "Race Condition Workflow",
        "scenario": "test",
        "state": "CREATED",
        "version": 1,
    }
    await firestore_store.save_workflow(wf_data)

    # Worker A reads version 1
    worker_a_view = await firestore_store.get_workflow(workflow_id)
    assert worker_a_view["version"] == 1

    # Worker B updates to version 2
    worker_b_view = await firestore_store.get_workflow(workflow_id)
    worker_b_view["state"] = "EXECUTING"
    await firestore_store.save_workflow(worker_b_view, expected_version=1)

    # Verify Worker B write succeeded
    b_updated = await firestore_store.get_workflow(workflow_id)
    assert b_updated["version"] == 2
    assert b_updated["state"] == "EXECUTING"

    # Worker A attempts stale write using version 1
    worker_a_view["state"] = "RECOVERING"
    with pytest.raises(StaleWorkflowStateError):
        await firestore_store.save_workflow(worker_a_view, expected_version=1)

    # Verify state remains at version 2 (Worker B state not overwritten)
    final_view = await firestore_store.get_workflow(workflow_id)
    assert final_view["version"] == 2
    assert final_view["state"] == "EXECUTING"


@pytest.mark.asyncio
async def test_firestore_store_recreation_survival():
    """
    Test persistence across actual store instance recreation:
    Create workflow -> write events, plans, idempotency, approvals -> destroy instance -> recreate -> reload.
    """
    workflow_id = str(uuid.uuid4())
    store1 = FirestoreWorkflowStore(project_id="recoveryos-test")

    # 1. Create workflow
    await store1.save_workflow({
        "workflow_id": workflow_id,
        "name": "Store Recreation Test",
        "scenario": "test",
        "state": "EXECUTING",
        "version": 1,
    })

    # 2. Append event
    await store1.append_event(workflow_id, {
        "event_id": f"ev-{uuid.uuid4().hex[:8]}",
        "workflow_id": workflow_id,
        "event_type": "STATE_CHANGE",
        "title": "Created",
        "detail": "Test detail",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # 3. Save recovery plan
    plan_id = f"plan-{uuid.uuid4().hex[:8]}"
    await store1.save_recovery_plan(workflow_id, {
        "plan_id": plan_id,
        "workflow_id": workflow_id,
        "target_outcome_id": "billing_configured",
        "diagnosis": "Stripe down",
        "status": "PROPOSED",
        "proposed_steps": [{"tool_name": "setup_billing", "tool_args": {"provider": "paypal"}}],
    })

    # 4. Save idempotency record
    idem_key = f"idem:{workflow_id}:setup_billing:cust-001"
    await store1.save_idempotency_record(idem_key, {
        "idempotency_key": idem_key,
        "workflow_id": workflow_id,
        "status": "SUCCEEDED",
        "result": {"status": "success", "subscription_id": "sub-123"},
    })

    # 5. Save human approval
    appr_id = f"appr-{uuid.uuid4().hex[:8]}"
    await store1.save_approval(workflow_id, {
        "approval_id": appr_id,
        "workflow_id": workflow_id,
        "action_tool": "setup_billing",
        "action_args": {"customer_id": "cust-001", "provider": "paypal"},
        "status": "APPROVED",
        "decided_by": "operator",
    })

    # Destroy store1 instance and create store2
    del store1
    store2 = FirestoreWorkflowStore(project_id="recoveryos-test")

    # Reload snapshot and assert everything survived
    snapshot = await store2.get_workflow_snapshot(workflow_id)
    assert snapshot is not None
    assert snapshot["workflow"]["workflow_id"] == workflow_id
    assert snapshot["workflow"]["state"] == "EXECUTING"
    assert len(snapshot["events"]) >= 1
    assert len(snapshot["recovery_plans"]) >= 1
    assert snapshot["recovery_plans"][0]["plan_id"] == plan_id
    assert len(snapshot["approvals"]) >= 1
    assert snapshot["approvals"][0]["approval_id"] == appr_id

    idem = await store2.get_idempotency_record(idem_key)
    assert idem is not None
    assert idem["status"] == "SUCCEEDED"
    assert idem["result"]["subscription_id"] == "sub-123"
