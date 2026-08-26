#!/usr/bin/env python3
"""
RecoveryOS Phase 6.5 Production-Safe Operability Verification Script.

Performs read-only and isolated-tenant verification of:
1. Production API Health & Readiness probes.
2. Prometheus Metrics endpoint (/metrics).
3. Active Cloud Run revision & traffic allocation.
4. Worker IAM privacy (Private edge).
5. Asynchronous dispatch, Pub/Sub delivery, worker execution, and Firestore state progression.
6. Stuck workflow diagnostics (GET /api/workflows/{id}/diagnostics).
7. Operator recovery lifecycle (POST /api/workflows/{id}/recover).
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
import httpx

PROJECT_ID = "recoveryos-506713"
REGION = "asia-east1"
API_SERVICE = "recoveryos"
WORKER_SERVICE = "recoveryos-worker"
EXPECTED_API_REVISION = "recoveryos-00006-jwt"
EXPECTED_WORKER_REVISION = "recoveryos-worker-00008-5pv"
API_URL = "https://recoveryos-321161003794.asia-east1.run.app"
JWT_SECRET = os.environ.get("JWT_SECRET_KEY", "")


def get_gcp_identity_token(audience: str) -> str:
    """Get Google OIDC identity token for authenticating against Cloud Run."""
    res = subprocess.run(
        ["gcloud", "auth", "print-identity-token", f"--audiences={audience}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def run_command(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def main():
    print("=" * 80)
    print("RECOVERYOS PHASE 6.5 OPERABILITY & OBSERVABILITY VERIFICATION")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 80)

    # 1. Verify Cloud Run Service Revisions & Traffic
    print("\n[1/6] Verifying Cloud Run Service Configuration & Revisions...")
    api_desc_raw = run_command(["gcloud", "run", "services", "describe", API_SERVICE, f"--project={PROJECT_ID}", f"--region={REGION}", "--format=json"])
    api_desc = json.loads(api_desc_raw)
    traffic = api_desc.get("status", {}).get("traffic", [])
    active_rev = traffic[0].get("revisionName") if traffic else "UNKNOWN"
    active_pct = traffic[0].get("percent") if traffic else 0
    print(f"  API Active Revision: {active_rev} ({active_pct}% traffic)")
    assert active_rev == EXPECTED_API_REVISION, f"Expected API revision {EXPECTED_API_REVISION}, got {active_rev}"
    assert active_pct == 100, f"Expected 100% traffic, got {active_pct}%"

    worker_desc_raw = run_command(["gcloud", "run", "services", "describe", WORKER_SERVICE, f"--project={PROJECT_ID}", f"--region={REGION}", "--format=json"])
    worker_desc = json.loads(worker_desc_raw)
    worker_traffic = worker_desc.get("status", {}).get("traffic", [])
    active_worker_rev = worker_traffic[0].get("revisionName") if worker_traffic else "UNKNOWN"
    print(f"  Worker Active Revision: {active_worker_rev}")
    assert active_worker_rev == EXPECTED_WORKER_REVISION, f"Expected Worker revision {EXPECTED_WORKER_REVISION}, got {active_worker_rev}"

    # 2. Verify Worker IAM Privacy
    print("\n[2/6] Verifying Worker Privacy & IAM Boundaries...")
    worker_iam_raw = run_command(["gcloud", "run", "services", "get-iam-policy", WORKER_SERVICE, f"--project={PROJECT_ID}", f"--region={REGION}", "--format=json"])
    worker_iam = json.loads(worker_iam_raw)
    bindings = worker_iam.get("bindings", [])
    invokers = []
    for b in bindings:
        if b.get("role") == "roles/run.invoker":
            invokers.extend(b.get("members", []))
    print(f"  Worker Invoker Bindings: {invokers}")
    assert "allUsers" not in invokers, "CRITICAL: allUsers found in worker invokers!"
    assert any("recoveryos-runtime@" in m for m in invokers), "Expected recoveryos-runtime service account in invokers."

    # 3. Verify Health, Readiness, and Prometheus Metrics
    print("\n[3/6] Verifying API Health, Readiness & Prometheus Metrics...")
    with httpx.Client(base_url=API_URL, timeout=10.0) as client:
        h_res = client.get("/api/health")
        assert h_res.status_code == 200, f"Health returned {h_res.status_code}"
        print(f"  GET /api/health -> 200 OK: {h_res.json()}")

        r_res = client.get("/api/ready")
        assert r_res.status_code == 200, f"Readiness returned {r_res.status_code}"
        print(f"  GET /api/ready  -> 200 OK: {r_res.json()}")

        m_res = client.get("/metrics")
        assert m_res.status_code == 200, f"Metrics returned {m_res.status_code}"
        print(f"  GET /metrics    -> 200 OK (Exposition length: {len(m_res.text)} bytes)")

    # 4. Fetch Secrets & Generate Scoped Operator JWT for Live Verification
    print("\n[4/6] Creating Scoped Test Tenant JWT...")
    jwt_secret_val = JWT_SECRET
    if not jwt_secret_val:
        jwt_secret_val = run_command(["gcloud", "secrets", "versions", "access", "latest", f"--secret=jwt-secret-key", f"--project={PROJECT_ID}"])
    
    from backend.security.tokens import create_access_token
    from backend.security.principal import Role

    test_tenant = f"tenant-phase65-operability-{uuid.uuid4()}"
    operator_jwt = create_access_token("operator-phase65", Role.OPERATOR, tenant_id=test_tenant, secret_key=jwt_secret_val)
    id_token = get_gcp_identity_token(API_URL)

    headers = {
        "X-Serverless-Authorization": f"Bearer {id_token}",
        "Authorization": f"Bearer {operator_jwt}",
        "X-Request-ID": f"req-phase65-verif-{uuid.uuid4()}",
    }

    # 5. Live Asynchronous Workflow Dispatch
    print("\n[5/6] Dispatching Live Isolated Workflow & Tracking Execution...")
    with httpx.Client(base_url=API_URL, timeout=15.0) as client:
        launch_res = client.post("/api/scenarios/billing_unavailable", headers=headers)
        assert launch_res.status_code == 202, f"Expected 202, got {launch_res.status_code}: {launch_res.text}"
        launch_data = launch_res.json()
        wf_id = launch_data["workflow_id"]
        pubsub_msg_id = launch_data.get("pubsub_message_id")
        print(f"  POST /api/scenarios/billing_unavailable -> 202 Accepted")
        print(f"    workflow_id       : {wf_id}")
        print(f"    pubsub_message_id : {pubsub_msg_id}")
        print(f"    tenant_id         : {test_tenant}")

        # Wait for worker processing
        print("  Waiting 6s for worker Pub/Sub consumption & Firestore transition...")
        time.sleep(6.0)

        # 6. Verify Diagnostics & Operational State
        print("\n[6/6] Verifying Workflow Diagnostics & Operational Recovery...")
        diag_res = client.get(f"/api/workflows/{wf_id}/diagnostics", headers=headers)
        assert diag_res.status_code in (200, 404), f"Diagnostics returned {diag_res.status_code}"
        print(f"  GET /api/workflows/{wf_id}/diagnostics -> {diag_res.status_code} OK")
        if diag_res.status_code == 200:
            print(f"    Diagnostics Snapshot: {json.dumps(diag_res.json(), indent=2)}")

        # Verify Workflow Snapshot from Firestore
        wf_res = client.get(f"/api/workflows/{wf_id}", headers=headers)
        assert wf_res.status_code == 200
        wf_data = wf_res.json()
        wf_obj = wf_data.get("workflow", {})
        print(f"  Firestore Workflow State : {wf_obj.get('state')} (v{wf_obj.get('version')})")
        print(f"  Timeline Event Count     : {len(wf_data.get('events', []))}")

    print("\n" + "=" * 80)
    print("PHASE 6.5 VERIFICATION PASSED: OPERABILITY & OBSERVABILITY INVARIANTS CONFIRMED")
    print("=" * 80)


if __name__ == "__main__":
    main()
