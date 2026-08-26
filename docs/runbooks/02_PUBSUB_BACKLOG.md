# Runbook 02: Pub/Sub Backlog & Delivery Delays

## 1. Symptoms
- Undelivered message count on `recoveryos-workflow-execution-worker` exceeds 50.
- Workflow progression from `CREATED` to `EXECUTING` experiences $>30\text{s}$ delays.

## 2. Diagnosis & Inspection Commands
```bash
export PATH="/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin:$PATH"

# 1. Inspect subscription details and backlog count
gcloud pubsub subscriptions describe recoveryos-workflow-execution-worker \
  --project=recoveryos-506713 \
  --format="json(pushConfig,ackDeadlineSeconds,state)"

# 2. Check if push endpoint is rejecting messages
gcloud logging read 'resource.labels.service_name="recoveryos-worker" AND textPayload:"429"' \
  --project=recoveryos-506713 \
  --limit=10
```

## 3. Decision Criteria
- If push deliveries are throttled by Cloud Run concurrency or max-instances, adjust instance ceiling.
- If messages are failing due to ack deadline expiration, verify worker processing latency.

## 4. Remediation Steps
```bash
# If worker needs higher throughput ceiling:
gcloud run services update recoveryos-worker \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --max-instances=3
```

## 5. Verification
- Monitor `num_undelivered_messages` until backlog drains to $< 5$.
- Confirm incoming workflows transition to `EXECUTING` within 5 seconds of dispatch.

## 6. Escalation Condition
- Backlog continues to grow $> 200$ messages or delivery latency $> 5$ minutes.
