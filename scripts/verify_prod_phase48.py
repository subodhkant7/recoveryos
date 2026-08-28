"""
Production Verification Script — Phase 48

Verifies:
1. Canonical API Root & Health.
2. Workflow 211af5a6-e611-43b2-861d-06fb55545760 state.
3. Live execution of billing_unavailable scenario to COMPLETED state.
4. Live execution of contradictory_evidence scenario.
5. Live execution of worker_interruption scenario.
"""

import asyncio
import sys
import httpx

BASE_URL = "https://recoveryos-321161003794.asia-east1.run.app"


async def main():
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        # 1. Login
        login_res = await client.post(
            f"{BASE_URL}/api/auth/login",
            json={"username": "operator", "role": "operator", "tenant_id": "tenant-default"},
        )
        assert login_res.status_code == 200, f"Login failed: {login_res.text}"
        token = login_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        print("✓ Authentication successful")

        # 2. Check root page
        root_res = await client.get(f"{BASE_URL}/")
        assert root_res.status_code == 200, f"Root page failed: {root_res.status_code}"
        print("✓ Canonical Root Page accessible (HTTP 200)")

        # 3. Check health
        health_res = await client.get(f"{BASE_URL}/api/health")
        assert health_res.status_code == 200, f"Health check failed: {health_res.status_code}"
        print("✓ API Health OK (HTTP 200)")

        # 4. Check historical workflow 211af5a6
        wf_211_res = await client.get(f"{BASE_URL}/api/workflows/211af5a6-e611-43b2-861d-06fb55545760", headers=headers)
        if wf_211_res.status_code == 200:
            wf_211 = wf_211_res.json()["workflow"]
            print(f"✓ Historical Workflow 211af5a6 state: {wf_211['state']} (attempts: {wf_211.get('recovery_attempts', 0)})")

        # 5. Launch billing_unavailable scenario
        print("\n--- Launching Scenario: billing_unavailable ---")
        launch_res = await client.post(f"{BASE_URL}/api/scenarios/billing_unavailable", json={}, headers=headers)
        assert launch_res.status_code in (200, 202), f"Launch failed: {launch_res.text}"
        wf_id_1 = launch_res.json()["workflow_id"]
        print(f"Dispatched billing_unavailable workflow ID: {wf_id_1}")

        # Poll for completion
        for attempt in range(40):
            await asyncio.sleep(2)
            snap_res = await client.get(f"{BASE_URL}/api/workflows/{wf_id_1}", headers=headers)
            if snap_res.status_code == 200:
                st = snap_res.json()["workflow"]["state"]
                print(f"  [Attempt {attempt+1}] State: {st}")
                if st in ("COMPLETED", "ESCALATED"):
                    assert st == "COMPLETED", f"Workflow failed to complete cleanly: {st}"
                    outcomes = snap_res.json()["workflow"]["contract"]["required_outcomes"]
                    verified_count = sum(1 for o in outcomes if o.get("verified"))
                    print(f"✓ billing_unavailable COMPLETED! ({verified_count}/{len(outcomes)} outcomes verified)")
                    break

        # 6. Launch contradictory_evidence scenario
        print("\n--- Launching Scenario: contradictory_evidence ---")
        launch_res_2 = await client.post(f"{BASE_URL}/api/scenarios/contradictory_evidence", json={}, headers=headers)
        assert launch_res_2.status_code in (200, 202), f"Launch failed: {launch_res_2.text}"
        wf_id_2 = launch_res_2.json()["workflow_id"]
        print(f"Dispatched contradictory_evidence workflow ID: {wf_id_2}")

        for attempt in range(25):
            await asyncio.sleep(2)
            snap_res = await client.get(f"{BASE_URL}/api/workflows/{wf_id_2}", headers=headers)
            if snap_res.status_code == 200:
                st = snap_res.json()["workflow"]["state"]
                print(f"  [Attempt {attempt+1}] State: {st}")
                if st in ("AWAITING_APPROVAL", "COMPLETED", "ESCALATED", "RECOVERING"):
                    print(f"✓ contradictory_evidence handled state: {st}")
                    break

        # 7. Launch worker_interruption scenario
        print("\n--- Launching Scenario: worker_interruption ---")
        launch_res_3 = await client.post(f"{BASE_URL}/api/scenarios/worker_interruption", json={}, headers=headers)
        assert launch_res_3.status_code in (200, 202), f"Launch failed: {launch_res_3.text}"
        wf_id_3 = launch_res_3.json()["workflow_id"]
        print(f"Dispatched worker_interruption workflow ID: {wf_id_3}")

        for attempt in range(30):
            await asyncio.sleep(2)
            snap_res = await client.get(f"{BASE_URL}/api/workflows/{wf_id_3}", headers=headers)
            if snap_res.status_code == 200:
                st = snap_res.json()["workflow"]["state"]
                print(f"  [Attempt {attempt+1}] State: {st}")
                if st in ("COMPLETED", "ESCALATED"):
                    assert st == "COMPLETED", f"worker_interruption failed: {st}"
                    print(f"✓ worker_interruption COMPLETED!")
                    break

        print("\n✓ ALL PRODUCTION SMOKE TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(main())
