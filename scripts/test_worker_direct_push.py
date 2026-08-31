import asyncio
import base64
import json
import shutil
import subprocess
import httpx
from google.oauth2 import credentials
from backend.events.message_models import WorkflowExecutionMessage, WorkflowEventType

SDK_GCLOUD = shutil.which("gcloud") or "gcloud"
WORKER_URL = "https://recoveryos-worker-321161003794.asia-east1.run.app"

id_token = subprocess.check_output([SDK_GCLOUD, "auth", "print-identity-token"]).decode().strip()

async def test_worker_direct():
    msg = WorkflowExecutionMessage(
        message_id="diag-msg-001",
        event_type=WorkflowEventType.WORKFLOW_DISPATCH,
        schema_version="1.0.0",
        tenant_id="tenant-acme",
        workflow_id="test-diag-001",
        idempotency_key="op-diag-001",
        expected_version=1,
        producer_id="recoveryos-api",
    )
    
    b64_payload = base64.b64encode(msg.to_pubsub_json().encode()).decode()
    envelope = {
        "message": {
            "data": b64_payload,
            "messageId": "diag-msg-001",
            "attributes": msg.to_pubsub_attributes(),
        },
        "subscription": "recoveryos-workflow-execution-worker",
    }
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{WORKER_URL}/",
            json=envelope,
            headers={"Authorization": f"Bearer {id_token}"},
        )
        print(f"Direct Push Response: status={res.status_code}, body={res.text}")

if __name__ == "__main__":
    asyncio.run(test_worker_direct())
