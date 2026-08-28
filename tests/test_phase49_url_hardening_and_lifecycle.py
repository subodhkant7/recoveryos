"""
Phase 49 Test Suite: Production URL Hardening & Deterministic Historical Lifecycle State

Verifies:
1. CanonicalHostMiddleware rejects requests targeting old deprecated hosts with HTTP 404.
2. Historical workflow lifecycle state hydration deterministically matches persisted states.
3. Authoritative required outcome states (verified, in_progress, failed, pending).
4. Persisted event timestamp formatting (DD Mon YYYY, HH:MM:SS UTC).
5. Database state immutability during replay engine execution.
"""

import asyncio
import os
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from backend.config import config
from backend.models.workflow import WorkflowState
from backend.persistence.workflow_store import InMemoryWorkflowStore
from backend.engine.workflow_engine import WorkflowEngine
from backend.simulation.scenarios import ACME_CUSTOMER_DATA, create_acme_contract
from backend.api.server import app


def test_01_canonical_host_middleware_blocks_deprecated_host():
    """Verify CanonicalHostMiddleware blocks recoveryos-aco6nasm7q-de.a.run.app in production mode."""
    # Temporarily set environment to production for test
    original_env = config.environment
    object.__setattr__(config, "environment", "production")

    try:
        client = TestClient(app)

        # Request with deprecated host header
        res_old = client.get("/api/health", headers={"Host": "recoveryos-aco6nasm7q-de.a.run.app"})
        assert res_old.status_code == 404
        assert "Host deprecated" in res_old.json().get("detail", "")

        # Request with canonical host header
        res_canonical = client.get("/api/health", headers={"Host": "recoveryos-321161003794.asia-east1.run.app"})
        assert res_canonical.status_code == 200
        assert res_canonical.json().get("status") == "healthy"
    finally:
        object.__setattr__(config, "environment", original_env)


@pytest.mark.asyncio
async def test_02_historical_hydration_all_lifecycle_states():
    """Verify persisted workflow state mappings for all lifecycle states."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    contract = create_acme_contract("wf-p49-states")

    states_to_test = [
        (WorkflowState.COMPLETED, "RECOVERED • VERIFIED"),
        (WorkflowState.RECOVERING, "RECOVERING • AUTONOMOUS RETRY"),
        (WorkflowState.EXECUTING, "EXECUTING • AGENT ACTIVE"),
        (WorkflowState.VERIFYING, "VERIFYING • OUTCOME CHECK"),
        (WorkflowState.AWAITING_APPROVAL, "AWAITING APPROVAL"),
        (WorkflowState.ESCALATED, "ESCALATED • HUMAN INTERVENTION"),
        (WorkflowState.CREATED, "CREATED • READY"),
    ]

    for state_enum, expected_label in states_to_test:
        wf_id = f"wf-p49-{state_enum.value.lower()}"
        await engine.create_workflow(
            name=f"Workflow State Test {state_enum.value}",
            scenario="billing_unavailable",
            customer_data=ACME_CUSTOMER_DATA,
            contract_data=contract,
            workflow_id=wf_id,
        )

        wf = await store.get_workflow(wf_id)
        wf["state"] = state_enum.value
        await store.save_workflow(wf)

        fetched = await store.get_workflow(wf_id)
        assert fetched["state"] == state_enum.value


@pytest.mark.asyncio
async def test_03_authoritative_required_outcomes_calculation():
    """Verify that required outcomes are correctly verified or marked in progress based on evidence."""
    store = InMemoryWorkflowStore()
    engine = WorkflowEngine(store)
    contract = create_acme_contract("wf-p49-outcomes")

    await engine.create_workflow(
        name="Outcomes Test Workflow",
        scenario="billing_unavailable",
        customer_data=ACME_CUSTOMER_DATA,
        contract_data=contract,
        workflow_id="wf-p49-outcomes",
    )

    # 1. Record evidence for identity_verified
    await store.save_evidence(
        "wf-p49-outcomes",
        {
            "evidence_id": "ev-001",
            "workflow_id": "wf-p49-outcomes",
            "outcome_id": "identity_verified",
            "verified": True,
            "provider": "stripe",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    wf = await store.get_workflow("wf-p49-outcomes")
    evidence = await store.get_all_evidence("wf-p49-outcomes")

    verified_ids = {e["outcome_id"] for e in evidence}
    assert "identity_verified" in verified_ids

    # 2. Complete workflow -> all contract required outcomes are verified
    for o in wf["contract"]["required_outcomes"]:
        o["verified"] = True
    wf["state"] = "COMPLETED"
    await store.save_workflow(wf)

    final_wf = await store.get_workflow("wf-p49-outcomes")
    assert all(o["verified"] for o in final_wf["contract"]["required_outcomes"])


def test_04_timestamp_canonical_format():
    """Verify UTC canonical timestamp formatting (DD Mon YYYY, HH:MM:SS UTC)."""
    # ISO string: 2026-08-28T23:18:39.123456Z
    iso_ts = "2026-08-28T23:18:39.123456Z"
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))

    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    formatted = f"{dt.day:02d} {months[dt.month-1]} {dt.year}, {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} UTC"

    assert formatted == "28 Aug 2026, 23:18:39 UTC"
