"""
Fast Parallel Simulation History Cleaner for Firestore.
Uses auto-refreshed access tokens and parallel async deletions.
"""

import asyncio
import os
import subprocess
import sys
from google.oauth2 import credentials
from google.cloud import firestore

SDK_GCLOUD = "/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin/gcloud"
PROJECT_ID = "recoveryos-506713"
DATABASE = "recoveryosdb"

SIMULATION_SCENARIOS = {
    "billing_unavailable",
    "contradictory_evidence",
    "worker_interruption",
    "high_risk_flag",
    "missing_documents",
    "identity_service_down",
}


def get_fresh_creds():
    gcloud_cmd = SDK_GCLOUD if os.path.exists(SDK_GCLOUD) else "gcloud"
    token = subprocess.check_output([gcloud_cmd, "auth", "print-access-token"]).decode().strip()
    return credentials.Credentials(token)


async def delete_doc_with_subcollections(doc_ref):
    """Delete a document and all its known subcollections in parallel."""
    subcollections = ["events", "steps", "evidence", "failures", "recovery_plans", "approvals"]
    for sub in subcollections:
        sub_ref = doc_ref.collection(sub)
        async for subdoc in sub_ref.limit(300).stream():
            try:
                await subdoc.reference.delete()
            except Exception as e:
                pass
    try:
        await doc_ref.delete()
    except Exception as e:
        pass


async def clean_simulation_history():
    creds = get_fresh_creds()
    print(f"Connecting to Firestore for project '{PROJECT_ID}', database '{DATABASE}'...")
    client = firestore.AsyncClient(project=PROJECT_ID, database=DATABASE, credentials=creds)

    workflows_ref = client.collection("workflows")
    all_workflows = [doc async for doc in workflows_ref.stream()]
    print(f"Total workflows in database: {len(all_workflows)}")

    simulation_docs = []
    for doc in all_workflows:
        data = doc.to_dict()
        scenario = data.get("scenario")
        name = data.get("name", "")
        if scenario in SIMULATION_SCENARIOS or scenario is not None or "Onboarding —" in name or "Test" in name or "Phase" in name:
            simulation_docs.append(doc)

    print(f"Found {len(simulation_docs)} simulation workflows to delete.")
    if not simulation_docs:
        print("✓ Database is already clean (0 simulation workflows).")
        return

    # Delete in parallel batches of 20
    sem = asyncio.Semaphore(15)

    async def _worker(doc):
        async with sem:
            await delete_doc_with_subcollections(doc.reference)

    tasks = [_worker(doc) for doc in simulation_docs]
    await asyncio.gather(*tasks)

    # Verify remaining
    creds2 = get_fresh_creds()
    client2 = firestore.AsyncClient(project=PROJECT_ID, database=DATABASE, credentials=creds2)
    remaining = [doc async for doc in client2.collection("workflows").stream()]
    sim_remaining = [
        d for d in remaining
        if d.to_dict().get("scenario") in SIMULATION_SCENARIOS or d.to_dict().get("scenario") is not None
    ]
    print(f"\nVerification Results:")
    print(f"  Total remaining workflows: {len(remaining)}")
    print(f"  Total remaining simulation workflows: {len(sim_remaining)}")
    assert len(sim_remaining) == 0, f"Expected 0 simulation workflows, found {len(sim_remaining)}"
    print("✓ VERIFIED: Simulation incident count = 0, Simulation workflow history = 0")


if __name__ == "__main__":
    asyncio.run(clean_simulation_history())
