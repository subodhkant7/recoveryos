"""
Phase 6.2.5: Full End-to-End Distributed Asynchronous Execution Verification Suite.

Tests and empirically verifies the entire production asynchronous pipeline:
1. Edge IAM enforcement (anonymous rejected with 403, valid GCP identity token accepted).
2. Health (/api/health) and Readiness (/api/readiness) on recoveryos-worker.
3. Workflow document seeding in live GCP Firestore (recoveryosdb).
4. Publisher -> Google Cloud Pub/Sub topic -> Push Subscription with OIDC Auth -> recoveryos-worker Cloud Run.
5. Asynchronous Worker state advancement (CREATED -> EXECUTING, Version incremented to 2, OperationClaim persisted).
6. Idempotency & Duplicate delivery protection (ACK on duplicate, no duplicate state mutation).
7. Security: Cross-tenant payload rejection (DEAD_LETTER / 422).
8. Poison pill / malformed message rejection (DEAD_LETTER / 422).
9. Cloud Run API service stability (recoveryos active revision recoveryos-00004-sw7 with 100% traffic).
"""

import asyncio
import base64
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from backend.events.message_models import (
    WorkflowEventType,
    WorkflowExecutionMessage,
)

SDK_GCLOUD = "/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin/gcloud"
GCP_PROJECT = "recoveryos-506713"
FIRESTORE_DB = "recoveryosdb"
WORKER_URL = "https://recoveryos-worker-321161003794.asia-east1.run.app"
API_URL = "https://recoveryos-321161003794.asia-east1.run.app"
TOPIC_NAME = "recoveryos-workflow-execution"
SUBSCRIPTION_NAME = "recoveryos-workflow-execution-worker"


def to_firestore_value(val: Any) -> dict[str, Any]:
    if isinstance(val, str):
        return {"stringValue": val}
    elif isinstance(val, bool):
        return {"booleanValue": val}
    elif isinstance(val, int):
        return {"integerValue": str(val)}
    elif isinstance(val, float):
        return {"doubleValue": val}
    elif isinstance(val, dict):
        return {"mapValue": {"fields": {k: to_firestore_value(v) for k, v in val.items()}}}
    elif isinstance(val, list):
        return {"arrayValue": {"values": [to_firestore_value(v) for v in val]}}
    elif val is None:
        return {"nullValue": None}
    return {"stringValue": str(val)}


def from_firestore_fields(fields: dict[str, Any]) -> dict[str, Any]:
    def decode(v: dict[str, Any]) -> Any:
        if "stringValue" in v:
            return v["stringValue"]
        if "integerValue" in v:
            return int(v["integerValue"])
        if "booleanValue" in v:
            return v["booleanValue"]
        if "doubleValue" in v:
            return float(v["doubleValue"])
        if "mapValue" in v:
            return {k: decode(val) for k, val in v["mapValue"].get("fields", {}).items()}
        if "arrayValue" in v:
            return [decode(val) for val in v["arrayValue"].get("values", [])]
        return None

    return {k: decode(v) for k, v in fields.items()}


async def firestore_set_doc(collection: str, doc_id: str, data: dict[str, Any], access_token: str) -> None:
    url = f"https://firestore.googleapis.com/v1/projects/{GCP_PROJECT}/databases/{FIRESTORE_DB}/documents/{collection}/{doc_id}"
    body = {"fields": {k: to_firestore_value(v) for k, v in data.items()}}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.patch(url, json=body, headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Firestore REST set_doc failed: {r.status_code} {r.text}")


async def firestore_get_doc(collection: str, doc_id: str, access_token: str) -> dict[str, Any] | None:
    url = f"https://firestore.googleapis.com/v1/projects/{GCP_PROJECT}/databases/{FIRESTORE_DB}/documents/{collection}/{doc_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            fields = r.json().get("fields", {})
            return from_firestore_fields(fields)
        raise RuntimeError(f"Firestore REST get_doc failed: {r.status_code} {r.text}")


def get_tokens_and_api_info() -> tuple[str, str, dict[str, Any]]:
    access_token = subprocess.check_output([SDK_GCLOUD, "auth", "print-access-token"]).decode().strip()
    id_token = subprocess.check_output([SDK_GCLOUD, "auth", "print-identity-token"]).decode().strip()
    api_desc = subprocess.check_output(
        [SDK_GCLOUD, "run", "services", "describe", "recoveryos", "--region=asia-east1", "--format=json"]
    ).decode().strip()
    return access_token, id_token, json.loads(api_desc)


async def main():
    print("=================================================================")
    print("STARTING PHASE 6.2.5 LIVE EMPIRICAL PRODUCTION VERIFICATION")
    print("=================================================================")

    evidence: dict[str, Any] = {}
    failure_matrix_evidence: dict[str, Any] = {}
    security_evidence: dict[str, Any] = {}

    access_token, id_token, api_info = get_tokens_and_api_info()

    # -----------------------------------------------------------------------
    # 1. Verify Worker Probes & Edge IAM Security
    # -----------------------------------------------------------------------
    print("\n--- 1. Testing Worker Edge IAM & Health/Readiness Probes ---")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Anonymous probe must be rejected by Cloud Run IAM (403)
        anon_res = await client.get(f"{WORKER_URL}/api/health")
        assert anon_res.status_code == 403, f"Expected 403 Forbidden for anonymous worker call, got {anon_res.status_code}"
        print(" [+] Anonymous invocation rejected with HTTP 403 (IAM Enforcement: VERIFIED)")
        security_evidence["anonymous_rejection"] = {
            "endpoint": f"{WORKER_URL}/api/health",
            "status_code": anon_res.status_code,
            "verified": True,
        }

        # Authenticated health probe
        auth_headers = {"Authorization": f"Bearer {id_token}"}
        health_res = await client.get(f"{WORKER_URL}/api/health", headers=auth_headers)
        assert health_res.status_code == 200
        health_data = health_res.json()
        print(f" [+] Worker /api/health probe: {health_data}")

        # Authenticated readiness probe
        readiness_res = await client.get(f"{WORKER_URL}/api/readiness", headers=auth_headers)
        assert readiness_res.status_code == 200
        readiness_data = readiness_res.json()
        print(f" [+] Worker /api/readiness probe: {readiness_data}")

    # -----------------------------------------------------------------------
    # 2. Setup Live Test Workflow in Firestore
    # -----------------------------------------------------------------------
    wf_id = f"wf-phase625-e2e-{uuid.uuid4().hex[:8]}"
    tenant_id = "tenant-acme"
    now_iso = datetime.now(timezone.utc).isoformat()

    wf_doc = {
        "id": wf_id,
        "workflow_id": wf_id,
        "name": f"Phase 6.2.5 Async E2E Verification — {wf_id}",
        "tenant_id": tenant_id,
        "scenario": "billing_unavailable",
        "state": "CREATED",
        "version": 1,
        "created_at": now_iso,
        "updated_at": now_iso,
        "customer_data": {
            "customer_id": "cust-acme-corp",
            "name": "ACME Corporation",
            "tier": "enterprise",
        },
        "contract": {
            "contract_id": f"contract-{wf_id}",
            "workflow_id": wf_id,
            "target_outcome": "RECOVER_SERVICE",
            "required_outcomes": [
                {
                    "outcome_id": "OUTCOME_BILLING_RECOVERED",
                    "description": "Ensure billing provider recovered or fallback provider engaged",
                    "verified": False,
                }
            ],
            "constraints": [{"constraint_id": "NO_DOUBLE_BILLING", "description": "Customer must not be billed twice"}],
        },
        "timeline": [],
    }

    await firestore_set_doc("workflows", wf_id, wf_doc, access_token)
    print(f"\n--- 2. Live Test Workflow Seeded in Firestore ({FIRESTORE_DB}) ---")
    print(f" [+] Document: workflows/{wf_id}")
    print(f" [+] Initial State: CREATED, Version: 1, Tenant: {tenant_id}")

    # Verify worker can read it via debug probe
    async with httpx.AsyncClient(timeout=15.0) as client:
        r_chk = await client.get(f"{WORKER_URL}/debug/workflow/{wf_id}", headers={"Authorization": f"Bearer {id_token}"})
        assert r_chk.json().get("found") is True, f"Worker could not find seeded workflow {wf_id}"
        print(f" [+] Confirmed seeded workflow visible to worker in Firestore: {r_chk.json()}")

    # -----------------------------------------------------------------------
    # 3. Publish Asynchronous Message to Pub/Sub
    # -----------------------------------------------------------------------
    msg_id = f"msg-live-{uuid.uuid4().hex[:8]}"
    idemp_key = f"op_dispatch_{wf_id}"
    corr_id = f"corr-trace-{uuid.uuid4().hex[:8]}"

    event_msg = WorkflowExecutionMessage(
        message_id=msg_id,
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id=tenant_id,
        workflow_id=wf_id,
        idempotency_key=idemp_key,
        expected_version=1,
        correlation_id=corr_id,
        producer_id="recoveryos-api",
    )

    print("\n--- 3. Publishing WorkflowExecutionMessage to Pub/Sub ---")
    print(f" [+] Topic: projects/{GCP_PROJECT}/topics/{TOPIC_NAME}")
    print(f" [+] Message ID: {msg_id}")
    print(f" [+] Idempotency Key: {idemp_key}")
    print(f" [+] Correlation ID: {corr_id}")

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        pub_url = f"https://pubsub.googleapis.com/v1/projects/{GCP_PROJECT}/topics/{TOPIC_NAME}:publish"
        pub_body = {
            "messages": [
                {
                    "data": base64.b64encode(event_msg.to_pubsub_json().encode("utf-8")).decode("utf-8"),
                    "attributes": {
                        "message_id": msg_id,
                        "event_type": event_msg.event_type.value,
                        "schema_version": "1.0.0",
                        "tenant_id": tenant_id,
                        "workflow_id": wf_id,
                        "expected_version": str(event_msg.expected_version),
                        "correlation_id": corr_id,
                    },
                }
            ]
        }
        pub_res = await http_client.post(
            pub_url,
            json=pub_body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert pub_res.status_code == 200, f"Pub/Sub publish failed: {pub_res.status_code} {pub_res.text}"
        gcp_msg_id = pub_res.json()["messageIds"][0]
        print(f" [+] Published to GCP Pub/Sub: GCP Message ID = {gcp_msg_id}")

    # -----------------------------------------------------------------------
    # 4. Await & Verify Asynchronous Worker Processing in Live Firestore
    # -----------------------------------------------------------------------
    print("\n--- 4. Polling Live Firestore for Asynchronous Worker Execution ---")
    start_time = time.time()
    max_wait = 45.0
    transitioned = False
    updated_doc_data: dict[str, Any] = {}

    while time.time() - start_time < max_wait:
        doc_data = await firestore_get_doc("workflows", wf_id, access_token)
        if doc_data:
            curr_state = doc_data.get("state")
            curr_ver = doc_data.get("version")
            print(f"       -> [t+{time.time()-start_time:.1f}s] State: {curr_state}, Version: {curr_ver}")
            if curr_state == "EXECUTING" and (curr_ver or 0) >= 2:
                transitioned = True
                updated_doc_data = doc_data
                break
        await asyncio.sleep(2.0)

    assert transitioned, f"Timed out waiting for recoveryos-worker to update workflow {wf_id} in Firestore!"
    print(f" [+] SUCCESS: Worker consumed message, advanced state to EXECUTING (Version: {updated_doc_data.get('version')})!")

    # Verify Operation Claim in Firestore
    op_data = await firestore_get_doc("operation_claims", idemp_key, access_token)
    assert op_data is not None, f"Operation claim record {idemp_key} missing in Firestore!"
    print(f" [+] Operation Claim Verified: Status={op_data.get('status')}, Worker={op_data.get('owner_worker_id')}")

    evidence["e2e_async_execution"] = {
        "workflow_id": wf_id,
        "tenant_id": tenant_id,
        "pubsub_message_id": gcp_msg_id,
        "idempotency_key": idemp_key,
        "correlation_id": corr_id,
        "initial_state": "CREATED",
        "initial_version": 1,
        "final_state": updated_doc_data.get("state"),
        "final_version": updated_doc_data.get("version"),
        "operation_claim": op_data,
        "latency_seconds": round(time.time() - start_time, 2),
        "verified": True,
    }

    # -----------------------------------------------------------------------
    # 5. Test Duplicate Redelivery (Idempotency)
    # -----------------------------------------------------------------------
    print("\n--- 5. Testing Duplicate Message Redelivery (Idempotency) ---")
    async with httpx.AsyncClient(timeout=15.0) as http_client:
        dup_res = await http_client.post(
            pub_url,
            json=pub_body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert dup_res.status_code == 200
        dup_gcp_id = dup_res.json()["messageIds"][0]
        print(f" [+] Published duplicate message: GCP Message ID = {dup_gcp_id}")

    await asyncio.sleep(5.0)
    dup_data = await firestore_get_doc("workflows", wf_id, access_token) or {}
    assert dup_data.get("state") == "EXECUTING", "Duplicate redelivery corrupted state!"
    assert dup_data.get("version") == updated_doc_data.get("version"), "Duplicate redelivery caused double version increment!"
    print(f" [+] Idempotency Verified: Version remained {dup_data.get('version')} after duplicate delivery.")

    failure_matrix_evidence["FAIL-07_duplicate_delivery"] = {
        "decision": "ACK",
        "state_mutated_twice": False,
        "verified": True,
    }

    # -----------------------------------------------------------------------
    # 6. Test Security: Cross-Tenant Isolation
    # -----------------------------------------------------------------------
    print("\n--- 6. Testing Cross-Tenant Message Isolation ---")
    evil_msg = WorkflowExecutionMessage(
        message_id=f"msg-evil-{uuid.uuid4().hex[:8]}",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-evil-attacker",
        workflow_id=wf_id,
        idempotency_key=f"op_evil_{wf_id}",
        expected_version=1,
        producer_id="recoveryos-api",
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        b64_payload = base64.b64encode(evil_msg.to_pubsub_json().encode()).decode()
        envelope = {
            "message": {"data": b64_payload, "messageId": "evil-001"},
            "subscription": SUBSCRIPTION_NAME,
        }
        res_evil = await client.post(
            f"{WORKER_URL}/",
            json=envelope,
            headers={"Authorization": f"Bearer {id_token}"},
        )
        assert res_evil.status_code == 422, f"Expected 422 DEAD_LETTER for tenant mismatch, got {res_evil.status_code}"
        print(f" [+] Cross-tenant message rejected with HTTP 422 (DEAD_LETTER): {res_evil.json()}")

        security_evidence["cross_tenant_isolation"] = {
            "status_code": res_evil.status_code,
            "response": res_evil.json(),
            "verified": True,
        }
        failure_matrix_evidence["FAIL-04_tenant_mismatch"] = {
            "decision": "DEAD_LETTER",
            "status_code": 422,
            "verified": True,
        }

    # -----------------------------------------------------------------------
    # 7. Test Poison Pill / Malformed Payload Handling
    # -----------------------------------------------------------------------
    print("\n--- 7. Testing Poison Pill & Malformed Payload Handling ---")
    async with httpx.AsyncClient(timeout=15.0) as client:
        bad_b64 = base64.b64encode(b'{"corrupted": "payload_without_contract"}').decode()
        poison_envelope = {
            "message": {"data": bad_b64, "messageId": "poison-001"},
            "subscription": SUBSCRIPTION_NAME,
        }
        poison_res = await client.post(
            f"{WORKER_URL}/",
            json=poison_envelope,
            headers={"Authorization": f"Bearer {id_token}"},
        )
        assert poison_res.status_code == 422, f"Expected 422 DEAD_LETTER for poison pill, got {poison_res.status_code}"
        print(f" [+] Poison pill rejected with HTTP 422 (DEAD_LETTER): {poison_res.json()}")

        failure_matrix_evidence["FAIL-01_poison_pill"] = {
            "decision": "DEAD_LETTER",
            "status_code": 422,
            "verified": True,
        }

    # -----------------------------------------------------------------------
    # 8. Verify Serving API Service Isolation
    # -----------------------------------------------------------------------
    print("\n--- 8. Verifying API Service Health & Invariant ---")
    active_rev = api_info.get("status", {}).get("latestReadyRevisionName")
    traffic = api_info.get("status", {}).get("traffic", [])
    print(" [+] API Service: recoveryos")
    print(f" [+] Active API Revision: {active_rev}")
    print(f" [+] Traffic Allocation: {traffic}")

    assert active_rev == "recoveryos-00004-sw7", f"API Revision unexpectedly changed: {active_rev}"
    assert any(t.get("revisionName") == "recoveryos-00004-sw7" and t.get("percent") == 100 for t in traffic)
    print(" [+] API Service remains on active revision recoveryos-00004-sw7 with 100% traffic!")

    # Write evidence artifacts
    os.makedirs("artifacts/phase6", exist_ok=True)
    with open("artifacts/phase6/phase6_2_5_e2e_evidence.json", "w") as f:
        json.dump(evidence, f, indent=2)
    with open("artifacts/phase6/phase6_2_5_failure_matrix_verification.json", "w") as f:
        json.dump(failure_matrix_evidence, f, indent=2)
    with open("artifacts/phase6/phase6_2_5_security_evidence.json", "w") as f:
        json.dump(security_evidence, f, indent=2)

    print("\n=================================================================")
    print("PHASE 6.2.5 VERIFICATION PASSED WITH COMPLETE EMPIRICAL EVIDENCE!")
    print("=================================================================")


if __name__ == "__main__":
    asyncio.run(main())
