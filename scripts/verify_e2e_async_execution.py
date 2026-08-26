"""
Live End-to-End Asynchronous Workflow Execution Verification Script for Phase 6.2.5.

Exercises:
1. API Service (recoveryos) -> Launches workflow in Firestore (State: RUNNING, Version: 1)
2. Publishes WorkflowExecutionMessage to Pub/Sub topic (recoveryos-workflow-execution)
3. Authenticated Pub/Sub push subscription delivers message to Worker Service (recoveryos-worker)
4. Worker Service validates provenance, claims operation, updates Firestore (State: EXECUTING, Version: 2)
5. API Service verifies transitioned state and OCC version
6. Duplicate redelivery test verifies idempotency (no double-mutation)
"""

import json
import os
import subprocess
import time
import uuid
import httpx

from backend.security.tokens import create_access_token
from backend.security.principal import Role
from backend.events.message_models import WorkflowExecutionMessage, WorkflowEventType


SDK_GCLOUD = "/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin/gcloud"
API_URL = "https://recoveryos-321161003794.asia-east1.run.app"
WORKER_URL = "https://recoveryos-worker-321161003794.asia-east1.run.app"
GCP_PROJECT = "recoveryos-506713"


def get_gcp_id_token():
    return subprocess.check_output([SDK_GCLOUD, "auth", "print-identity-token"]).decode().strip()


def get_jwt_secret():
    return subprocess.check_output([
        SDK_GCLOUD, "secrets", "versions", "access", "latest",
        "--secret=recoveryos-jwt-secret", f"--project={GCP_PROJECT}",
    ]).decode().strip()


def run_e2e_verification():
    print("=================================================================")
    print("PHASE 6.2.5: LIVE END-TO-END ASYNCHRONOUS VERIFICATION IN GCP")
    print("=================================================================")

    # 1. Obtain Tokens
    id_token = get_gcp_id_token()
    jwt_secret = get_jwt_secret()
    operator_jwt = create_access_token(
        user_id="e2e-worker-tester",
        role=Role.OPERATOR.value,
        tenant_id="tenant-acme",
        secret_key=jwt_secret,
    )
    print(" [1/6] Identity token and Operator JWT generated.")

    headers = {
        "X-Serverless-Authorization": f"Bearer {id_token}",
        "Authorization": f"Bearer {operator_jwt}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30.0) as client:
        # 2. Launch workflow on API service
        res = client.post(
            f"{API_URL}/api/scenarios/billing_unavailable",
            headers=headers,
        )
        assert res.status_code == 200, f"API launch failed: {res.status_code} {res.text}"
        wf_data = res.json()
        wf_id = wf_data["workflow_id"]
        init_state = wf_data.get("state")
        init_ver = wf_data.get("version")
        print(f" [2/6] Workflow launched on API Service: ID={wf_id}, State={init_state}, Version={init_ver}")

        # 3. Construct and Publish Message to Pub/Sub
        msg_id = f"msg-e2e-{uuid.uuid4().hex[:8]}"
        idemp_key = f"op_dispatch_{wf_id}"
        corr_id = f"corr-e2e-{uuid.uuid4().hex[:8]}"

        event_msg = WorkflowExecutionMessage(
            message_id=msg_id,
            event_type=WorkflowEventType.WORKFLOW_DISPATCH,
            schema_version="1.0.0",
            tenant_id="tenant-acme",
            workflow_id=wf_id,
            idempotency_key=idemp_key,
            expected_version=1,
            correlation_id=corr_id,
            producer_id="recoveryos-api",
        )

        pub_cmd = [
            SDK_GCLOUD, "pubsub", "topics", "publish", "recoveryos-workflow-execution",
            f"--message={event_msg.to_pubsub_json()}",
            f"--attribute=message_id={msg_id},event_type=WORKFLOW_DISPATCH,schema_version=1.0.0,tenant_id=tenant-acme,workflow_id={wf_id},expected_version=1,correlation_id={corr_id}",
            f"--project={GCP_PROJECT}",
        ]
        pub_out = subprocess.check_output(pub_cmd).decode()
        print(f" [3/6] Message published to Pub/Sub: {pub_out.strip()}")

        # 4. Poll for Asynchronous Execution by recoveryos-worker
        print(" [4/6] Polling API Service for asynchronous worker state transition in Firestore...")
        max_wait = 30
        start = time.time()
        transitioned = False
        final_wf = {}

        while time.time() - start < max_wait:
            headers["X-Serverless-Authorization"] = f"Bearer {get_gcp_id_token()}"
            get_res = client.get(f"{API_URL}/api/workflows/{wf_id}", headers=headers)
            if get_res.status_code == 200:
                snap = get_res.json()
                current_wf = snap.get("workflow", snap)
                st = current_wf.get("state")
                ver = current_wf.get("version")
                print(f"       -> Workflow State: {st}, Version: {ver} (elapsed: {time.time()-start:.1f}s)")
                if st == "EXECUTING" and ver >= 2:
                    transitioned = True
                    final_wf = current_wf
                    break
            time.sleep(2.0)

        assert transitioned, f"Timed out waiting for asynchronous worker transition on workflow {wf_id}"
        print(f" [5/6] SUCCESS: Asynchronous execution proven! New State: {final_wf.get('state')}, New Version: {final_wf.get('version')}")

        # 5. Duplicate Redelivery Test (Idempotency)
        print(" [6/6] Testing duplicate message redelivery idempotency...")
        dup_pub_out = subprocess.check_output(pub_cmd).decode()
        time.sleep(3.0)
        
        headers["X-Serverless-Authorization"] = f"Bearer {get_gcp_id_token()}"
        dup_check_res = client.get(f"{API_URL}/api/workflows/{wf_id}", headers=headers)
        assert dup_check_res.status_code == 200
        dup_snap = dup_check_res.json()
        dup_wf = dup_snap.get("workflow", dup_snap)
        assert dup_wf.get("state") == "EXECUTING"
        assert dup_wf.get("version") == final_wf.get("version"), "Duplicate message caused invalid state mutation!"
        print(f"       -> Duplicate redelivery handled idempotently (Version remained {dup_wf.get('version')}).")

    print("=================================================================")
    print("ALL PHASE 6.2.5 E2E ASYNCHRONOUS CHECKS PASSED EMPIRICALLY!")
    print("=================================================================")


if __name__ == "__main__":
    run_e2e_verification()
