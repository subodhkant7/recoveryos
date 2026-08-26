# Runbook 05: Firestore OCC Conflict Spike Resolution

## 1. Symptoms
- `recoveryos_occ_mismatches_total` metric increases rapidly.
- Worker logs show frequent `WORKER_OCC_CONFLICT` with message `OCC Version mismatch on event consumption`.

## 2. Diagnosis & Inspection Commands
```bash
export PATH="/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin:$PATH"

# Query OCC mismatch logs
gcloud logging read 'jsonPayload.event_name="WORKER_OCC_CONFLICT"' \
  --project=recoveryos-506713 \
  --limit=20 \
  --format="json(timestamp,jsonPayload.workflow_id,jsonPayload.current_version,jsonPayload.expected_version)"
```

## 3. Decision Criteria
- If multiple Pub/Sub messages were published concurrently for the same workflow with stale versions, verify client dispatch logic.
- If worker retries succeed automatically on second attempt, this is normal backoff behavior.

## 4. Remediation Steps
- Verify that API publishers fetch the latest workflow document version before constructing `WorkflowExecutionMessage`.
- Ensure clients do not issue overlapping parallel mutation requests against the same workflow instance.

## 5. Verification
- Confirm `recoveryos_occ_mismatches_total` rate drops back to baseline ($< 0.05/\text{sec}$).
- Check that workflows advance to their expected terminal states without data loss.

## 6. Escalation Condition
- Single workflow experiences $> 10$ continuous OCC version rejections.
