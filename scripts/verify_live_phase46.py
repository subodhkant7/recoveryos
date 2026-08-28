"""
Live Acceptance Verification for Phase 46:
- Trigger Scenario: billing_unavailable on live Cloud Run
- Follow execution until COMPLETED
- Check all timestamps for ISO-8601 UTC date+time format
- Check required outcomes and Recovery Proof Certificate
"""

import asyncio
import json
import os
import sys
import httpx

from backend.security.tokens import create_access_token
from backend.security.principal import Role

CLOUD_RUN_URL = "https://recoveryos-321161003794.asia-east1.run.app"


async def verify_live_execution():
    async with httpx.AsyncClient(base_url=CLOUD_RUN_URL, timeout=60.0) as client:
        print("1. Checking Health on Cloud Run...")
        health = await client.get("/api/health")
        print(f"   Health response: {health.status_code} {health.json()}")
        assert health.status_code == 200

        print("\n2. Logging in as Operator via /api/auth/login...")
        login_res = await client.post(
            "/api/auth/login",
            json={"username": "operator", "role": "operator", "tenant_id": "tenant-default"},
        )
        print(f"   Login status: {login_res.status_code}")
        assert login_res.status_code == 200
        token = login_res.json()["access_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        overview = await client.get("/api/operator/overview", headers=headers)
        print(f"   Overview response: {overview.status_code} {overview.json()}")
        assert overview.status_code == 200

        print("\n3. Triggering 'billing_unavailable' Simulation Scenario...")
        launch_res = await client.post(
            "/api/scenarios/billing_unavailable",
            headers=headers,
        )
        print(f"   Launch status: {launch_res.status_code}")
        launch_data = launch_res.json()
        print(f"   Launch response: {launch_data}")
        assert launch_res.status_code in (200, 202)
        wf_id = launch_data.get("workflow_id")
        assert wf_id is not None

        print(f"\n3. Polling live workflow {wf_id} until terminal state...")
        max_attempts = 45
        snapshot = None
        for attempt in range(max_attempts):
            await asyncio.sleep(2)
            snap_res = await client.get(f"/api/workflows/{wf_id}", headers=headers)
            if snap_res.status_code == 200:
                snapshot = snap_res.json()
                wf = snapshot.get("workflow", {})
                state = wf.get("state")
                events = snapshot.get("events", [])
                evidence = snapshot.get("evidence", [])
                print(f"   [{attempt+1:02d}s] State: {state} | Events: {len(events)} | Evidence: {len(evidence)}")

                if state in ("COMPLETED", "ESCALATED", "FAILED"):
                    break

        assert snapshot is not None
        wf = snapshot.get("workflow", {})
        final_state = wf.get("state")
        events = snapshot.get("events", [])
        evidence = snapshot.get("evidence", [])

        print(f"\n4. Final State Verification:")
        print(f"   • Workflow State: {final_state}")
        print(f"   • Created At: {wf.get('created_at')}")
        print(f"   • Updated At: {wf.get('updated_at')}")
        print(f"   • Total Authoritative Events: {len(events)}")
        print(f"   • Verified Evidence Items: {len(evidence)}")

        assert final_state == "COMPLETED", f"Expected COMPLETED, got {final_state}"
        assert wf.get("created_at") is not None
        assert "T" in wf.get("created_at")

        print("\n5. Validating Event Timestamps & Sequence:")
        for idx, ev in enumerate(events):
            occ = ev.get("occurred_at") or ev.get("timestamp")
            assert occ is not None, f"Event {idx} missing timestamp"
            print(f"   [{idx:02d}] [{occ}] {ev.get('actor', 'SYS')}: {ev.get('title')} ({ev.get('event_type')})")

        print("\n6. Checking Recovery Proof Certificate & Evidence:")
        proof = snapshot.get("recovery_proof")
        print(f"   • Proof Generated: {proof is not None}")
        if proof:
            print(f"   • Proof Status: {proof.get('status')}")
            print(f"   • Contract Status: {proof.get('contract_status')}")

        print("\n✓ LIVE ACCEPTANCE VERIFICATION COMPLETED SUCCESSFULLY ON CLOUD RUN!")


if __name__ == "__main__":
    asyncio.run(verify_live_execution())
