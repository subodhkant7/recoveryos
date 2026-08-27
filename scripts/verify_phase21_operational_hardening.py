"""
Phase 21: Operational Hardening & Worker Upgrade Readiness Audit Tool.

Non-mutating verification tool that checks:
1. Production API traffic allocation (100% on recoveryos-00019-vog).
2. Production API image digest match (sha256:cb43b57e04b2...).
3. Rollback reserve revision availability (recoveryos-00008-2bt).
4. Production Worker revision & health (recoveryos-worker-00008-5pv).
5. Production Pub/Sub & DLQ subscription topology.
6. Staging infrastructure isolation (zero cross-routing).
7. Authenticated live API health & security probes.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import httpx

EXPECTED_CANDIDATE_DIGEST = "sha256:cb43b57e04b208edd9e74f68e7e8d4dfb9bc96acf9012850ba2bcb406c71f13f"
PROD_API_URL = "https://recoveryos-321161003794.asia-east1.run.app"
PROD_WORKER_URL = "https://recoveryos-worker-321161003794.asia-east1.run.app"
STAGE_API_URL = "https://recoveryos-stage-321161003794.asia-east1.run.app"
STAGE_WORKER_URL = "https://recoveryos-worker-stage-321161003794.asia-east1.run.app"
GCP_PROJECT = "recoveryos-506713"
GCP_REGION = "asia-east1"


def run_gcloud(args: list[str]) -> str:
    res = subprocess.run(
        ["./google-cloud-sdk/bin/gcloud"] + args,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def get_gcp_id_token() -> str:
    return run_gcloud(["auth", "print-identity-token"])


async def main():
    print("=" * 80)
    print("RECOVERYOS PHASE 21: OPERATIONAL HARDENING AUDIT")
    print(f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print(f"GCP Project: {GCP_PROJECT} ({GCP_REGION})")
    print("=" * 80)

    results: dict[str, str] = {}

    # --------------------------------------------------------------------------
    # 1. Cloud Run API State & Digest Audit
    # --------------------------------------------------------------------------
    print("\n[1] Auditing Production API Service State...")
    svc_api_json = json.loads(run_gcloud([
        "run", "services", "describe", "recoveryos",
        f"--region={GCP_REGION}", f"--project={GCP_PROJECT}", "--format=json",
    ]))
    traffic = svc_api_json.get("status", {}).get("traffic", [])
    active_rev = traffic[0].get("revisionName")
    active_percent = traffic[0].get("percent")
    print(f"  Active API Revision: {active_rev} ({active_percent}% traffic)")
    assert active_rev == "recoveryos-00019-vog"
    assert active_percent == 100
    results["1. Production API Traffic"] = "PASS"

    rev_api_json = json.loads(run_gcloud([
        "run", "revisions", "describe", "recoveryos-00019-vog",
        f"--region={GCP_REGION}", f"--project={GCP_PROJECT}", "--format=json",
    ]))
    live_digest = rev_api_json.get("status", {}).get("imageDigest", "")
    print(f"  Image Digest: {live_digest}")
    assert EXPECTED_CANDIDATE_DIGEST in live_digest
    results["2. API Image Digest Provenance"] = "PASS"

    # --------------------------------------------------------------------------
    # 2. Rollback Reserve Availability
    # --------------------------------------------------------------------------
    print("\n[2] Checking Rollback Target Availability...")
    rev_rollback_json = json.loads(run_gcloud([
        "run", "revisions", "describe", "recoveryos-00008-2bt",
        f"--region={GCP_REGION}", f"--project={GCP_PROJECT}", "--format=json",
    ]))
    rollback_conditions = rev_rollback_json.get("status", {}).get("conditions", [])
    rollback_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in rollback_conditions)
    print(f"  Rollback Reserve (recoveryos-00008-2bt) Ready: {rollback_ready}")
    assert rollback_ready
    results["3. Rollback Reserve Ready"] = "PASS"

    # --------------------------------------------------------------------------
    # 3. Production Worker State Audit
    # --------------------------------------------------------------------------
    print("\n[3] Auditing Production Worker Service...")
    svc_worker_json = json.loads(run_gcloud([
        "run", "services", "describe", "recoveryos-worker",
        f"--region={GCP_REGION}", f"--project={GCP_PROJECT}", "--format=json",
    ]))
    worker_traffic = svc_worker_json.get("status", {}).get("traffic", [])
    active_worker_rev = worker_traffic[0].get("revisionName")
    print(f"  Active Worker Revision: {active_worker_rev}")
    assert active_worker_rev == "recoveryos-worker-00008-5pv"
    results["4. Production Worker Active"] = "PASS"

    # --------------------------------------------------------------------------
    # 4. Production Pub/Sub & DLQ Audit
    # --------------------------------------------------------------------------
    print("\n[4] Auditing Production Pub/Sub & DLQ Topology...")
    sub_worker_json = json.loads(run_gcloud([
        "pubsub", "subscriptions", "describe", "recoveryos-workflow-execution-worker",
        f"--project={GCP_PROJECT}", "--format=json",
    ]))
    push_endpoint = sub_worker_json.get("pushConfig", {}).get("pushEndpoint", "")
    dlq_topic = sub_worker_json.get("deadLetterPolicy", {}).get("deadLetterTopic", "")
    print(f"  Push Endpoint: {push_endpoint}")
    print(f"  DLQ Topic: {dlq_topic}")
    assert "recoveryos-worker" in push_endpoint
    assert "recoveryos-workflow-execution-dlq" in dlq_topic
    results["5. Production Pub/Sub Routing"] = "PASS"

    # --------------------------------------------------------------------------
    # 5. Staging Infrastructure Isolation Audit
    # --------------------------------------------------------------------------
    print("\n[5] Auditing Staging Isolation Boundaries...")
    sub_stage_json = json.loads(run_gcloud([
        "pubsub", "subscriptions", "describe", "recoveryos-workflow-execution-worker-stage",
        f"--project={GCP_PROJECT}", "--format=json",
    ]))
    stage_endpoint = sub_stage_json.get("pushConfig", {}).get("pushEndpoint", "")
    stage_topic = sub_stage_json.get("topic", "")
    print(f"  Staging Topic: {stage_topic}")
    print(f"  Staging Endpoint: {stage_endpoint}")
    assert "recoveryos-workflow-execution-stage" in stage_topic
    assert "recoveryos-worker-stage" in stage_endpoint
    assert stage_topic != sub_worker_json.get("topic")
    results["6. Staging Isolation"] = "PASS"

    # --------------------------------------------------------------------------
    # 6. Live API Authenticated Smoke Probe
    # --------------------------------------------------------------------------
    print("\n[6] Performing Live Authenticated API Probe...")
    id_token = get_gcp_id_token()
    gcp_headers = {"Authorization": f"Bearer {id_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{PROD_API_URL}/api/health", headers=gcp_headers)
        assert resp.status_code == 200
        print(f"  API Health: HTTP {resp.status_code} ({resp.json().get('status')})")

        resp_worker = await client.get(f"{PROD_WORKER_URL}/api/health", headers=gcp_headers)
        assert resp_worker.status_code == 200
        print(f"  Worker Health: HTTP {resp_worker.status_code} ({resp_worker.json().get('status')})")
        results["7. Live Service Probes"] = "PASS"

    print("\n" + "=" * 80)
    print("PHASE 21 OPERATIONAL AUDIT SUMMARY")
    print("=" * 80)
    for k, v in results.items():
        print(f"  {k:35s}: {v}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
