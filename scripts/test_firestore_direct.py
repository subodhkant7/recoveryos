import asyncio
import os
import shutil
import subprocess
from google.oauth2 import credentials
from backend.persistence.workflow_store import FirestoreWorkflowStore

SDK_GCLOUD = shutil.which("gcloud") or "gcloud"
GCP_PROJECT = "recoveryos-506713"
FIRESTORE_DB = "recoveryosdb"

token = subprocess.check_output([SDK_GCLOUD, "auth", "print-access-token"]).decode().strip()
creds = credentials.Credentials(token)

async def test():
    store = FirestoreWorkflowStore(project_id=GCP_PROJECT, database=FIRESTORE_DB, credentials=creds)
    
    wf_id = "test-diag-001"
    await store.save_workflow({
        "workflow_id": wf_id,
        "tenant_id": "tenant-acme",
        "state": "RUNNING",
        "version": 1,
    })
    
    doc = await store.get_workflow(wf_id)
    print("Direct Firestore Read via FirestoreWorkflowStore:", doc)
    assert doc is not None
    assert doc["workflow_id"] == wf_id
    print("SUCCESS: FirestoreWorkflowStore direct CRUD verified!")

if __name__ == "__main__":
    asyncio.run(test())
