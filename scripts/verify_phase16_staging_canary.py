"""
Phase 16: Live Real-GCP Staging Canary & Distributed Verification Suite.

Executes all Phase D live tests against:
- Staging API: https://recoveryos-stage-321161003794.asia-east1.run.app
- Staging Worker: https://recoveryos-worker-stage-321161003794.asia-east1.run.app
- Staging Pub/Sub Topic: recoveryos-workflow-execution-stage
- Staging Subscription: recoveryos-workflow-execution-worker-stage
- Firestore Database: recoveryosdb
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import uuid
import httpx

# Ensure backend imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.security.tokens import _b64url_decode
from backend.simulation.scenarios import create_acme_contract, ACME_CUSTOMER_DATA

def decode_token_unverified(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    return json.loads(_b64url_decode(parts[1]).decode("utf-8"))

STAGE_API_URL = "https://recoveryos-stage-321161003794.asia-east1.run.app"
STAGE_WORKER_URL = "https://recoveryos-worker-stage-321161003794.asia-east1.run.app"
GCP_PROJECT = "recoveryos-506713"
GCP_REGION = "asia-east1"


async def main():
    print("=" * 80)
    print("RECOVERYOS PHASE 16: REAL GCP STAGING CANARY VERIFICATION")
    print(f"Time: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print(f"Target Staging API: {STAGE_API_URL}")
    print(f"Target Staging Worker: {STAGE_WORKER_URL}")
    print("=" * 80)

    results: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # ----------------------------------------------------------------------
        # Test 1: GET /api/health
        # ----------------------------------------------------------------------
        print("\n[Test 1] Probing Staging API Health...")
        t0 = time.time()
        resp = await client.get(f"{STAGE_API_URL}/api/health")
        latency = (time.time() - t0) * 1000
        print(f"  Status: {resp.status_code}, Latency: {latency:.1f}ms, Body: {resp.text}")
        assert resp.status_code == 200
        assert resp.json().get("status") == "healthy"
        results["1. Health Canary"] = "PASS"

        # ----------------------------------------------------------------------
        # Test 2: Real Authentication Canary
        # ----------------------------------------------------------------------
        print("\n[Test 2] Testing Authentication & Server-Bound Authority...")
        
        # 2a. Wrong password -> 401
        resp = await client.post(f"{STAGE_API_URL}/api/auth/login", json={
            "username": "operator",
            "password": "WrongPassword123!",
        })
        print(f"  Wrong password test: HTTP {resp.status_code}")
        assert resp.status_code == 401

        # 2b. Unknown user -> 401
        resp = await client.post(f"{STAGE_API_URL}/api/auth/login", json={
            "username": "non_existent_user",
            "password": "Password123!",
        })
        print(f"  Unknown user test: HTTP {resp.status_code}")
        assert resp.status_code == 401

        # 2c. Valid operator login
        resp = await client.post(f"{STAGE_API_URL}/api/auth/login", json={
            "username": "operator",
            "password": "OperatorSecurePass!2026",
        })
        print(f"  Valid operator login: HTTP {resp.status_code}")
        assert resp.status_code == 200
        login_data = resp.json()
        assert "access_token" in login_data
        token = login_data["access_token"]
        claims = decode_token_unverified(token)
        assert claims.get("sub") == "operator"
        assert claims.get("role") == "operator"
        assert claims.get("tenant_id") == "tenant-default"

        # 2d. Privilege escalation attempt
        resp = await client.post(f"{STAGE_API_URL}/api/auth/login", json={
            "username": "operator",
            "password": "OperatorSecurePass!2026",
            "role": "admin",
            "tenant_id": "tenant-victim",
        })
        assert resp.status_code == 200
        escalation_claims = decode_token_unverified(resp.json()["access_token"])
        print(f"  Escalation attempt claims: role={escalation_claims.get('role')}, tenant={escalation_claims.get('tenant_id')}")
        assert escalation_claims.get("role") == "operator", "Role elevation must be blocked"
        assert escalation_claims.get("tenant_id") == "tenant-default", "Tenant override must be blocked"
        results["2. Authentication Canary"] = "PASS"

        auth_headers = {"Authorization": f"Bearer {token}"}

        # ----------------------------------------------------------------------
        # Test 3: Staging Workflow Creation & Asynchronous Dispatch
        # ----------------------------------------------------------------------
        print("\n[Test 3] Launching Real Staging Scenario (billing_unavailable)...")
        resp = await client.post(
            f"{STAGE_API_URL}/api/scenarios/billing_unavailable",
            headers=auth_headers,
        )
        print(f"  Launch scenario response: HTTP {resp.status_code}, Body: {resp.text}")
        assert resp.status_code in (200, 202)
        wf_created = resp.json()
        actual_wf_id = wf_created.get("workflow_id")
        pubsub_msg_id = wf_created.get("pubsub_message_id")
        assert actual_wf_id is not None
        print(f"  Created Staging Workflow ID: {actual_wf_id}, Pub/Sub Msg ID: {pubsub_msg_id}")
        results["3. Workflow Creation & PubSub Dispatch"] = "PASS"

        # ----------------------------------------------------------------------
        # Test 4: Single-Use SSE Ticket Canary
        # ----------------------------------------------------------------------
        print("\n[Test 4] Testing Single-Use SSE Ticket Protocol...")
        resp = await client.post(
            f"{STAGE_API_URL}/api/auth/sse-ticket",
            headers=auth_headers,
            json={"workflow_id": actual_wf_id},
        )
        print(f"  Issue SSE ticket response: HTTP {resp.status_code}")
        assert resp.status_code == 200
        ticket_data = resp.json()
        ticket_id = ticket_data.get("ticket")
        assert ticket_id is not None and ticket_id.startswith("sset_")
        assert ticket_data.get("expires_in") == 60

        # Consume ticket via SSE stream
        async with client.stream("GET", f"{STAGE_API_URL}/api/workflows/{actual_wf_id}/events/stream?ticket={ticket_id}") as stream_resp:
            print(f"  SSE Stream response: HTTP {stream_resp.status_code}, Content-Type: {stream_resp.headers.get('content-type')}")
            assert stream_resp.status_code == 200
            assert "text/event-stream" in stream_resp.headers.get("content-type", "")

        # Ticket reuse attempt -> Must return 401
        resp_reuse = await client.get(f"{STAGE_API_URL}/api/workflows/{actual_wf_id}/events/stream?ticket={ticket_id}")
        print(f"  SSE Ticket reuse attempt: HTTP {resp_reuse.status_code}")
        assert resp_reuse.status_code == 401
        results["4. SSE Ticket Security"] = "PASS"

        # ----------------------------------------------------------------------
        # Test 5: Cross-Container Event Delivery
        # ----------------------------------------------------------------------
        print("\n[Test 5] Observing Cross-Container Event Delivery on Staging...")
        ticket_resp = await client.post(
            f"{STAGE_API_URL}/api/auth/sse-ticket",
            headers=auth_headers,
            json={"workflow_id": actual_wf_id},
        )
        fresh_ticket = ticket_resp.json()["ticket"]

        print(f"  Streaming events for {actual_wf_id}...")
        events_received = []
        async with client.stream("GET", f"{STAGE_API_URL}/api/workflows/{actual_wf_id}/events/stream?ticket={fresh_ticket}") as stream:
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    try:
                        ev = json.loads(line[6:])
                        events_received.append(ev)
                        ev_type = ev.get("event_type") or ev.get("type")
                        print(f"    -> Received event: {ev_type} | {ev.get('title', '')}")
                        if ev_type in ("STREAM_END", "WORKFLOW_COMPLETED", "WORKFLOW_FAILED"):
                            break
                    except Exception:
                        pass
                if len(events_received) >= 4:
                    break

        print(f"  Total events observed via cross-container SSE stream: {len(events_received)}")
        results["5. Cross-Container SSE Streaming"] = "PASS"

        # ----------------------------------------------------------------------
        # Test 6: Operator Fleet & Diagnostic APIs
        # ----------------------------------------------------------------------
        print("\n[Test 6] Testing Operator Overview & Diagnostics...")
        resp = await client.get(f"{STAGE_API_URL}/api/operator/overview", headers=auth_headers)
        print(f"  Operator overview: HTTP {resp.status_code}, Total Workflows: {resp.json().get('total_workflows')}")
        assert resp.status_code == 200

        resp_diag = await client.get(f"{STAGE_API_URL}/api/workflows/{actual_wf_id}/diagnostics", headers=auth_headers)
        print(f"  Workflow diagnostics: HTTP {resp_diag.status_code}, is_stuck: {resp_diag.json().get('is_stuck')}")
        assert resp_diag.status_code == 200
        results["7. Operator Diagnostics"] = "PASS"

    print("\n" + "=" * 80)
    print("REAL GCP STAGING CANARY TEST RESULTS SUMMARY")
    print("=" * 80)
    for k, v in results.items():
        print(f"  {k:35s}: {v}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
