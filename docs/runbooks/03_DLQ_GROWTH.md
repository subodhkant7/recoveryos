# Runbook 03: Dead Letter Queue (DLQ) Accumulation

## 1. Symptoms
- Messages are present on `recoveryos-workflow-execution-dlq-sub`.
- `recoveryos_worker_executions_total{status="dead_letter"}` metric increments.

## 2. Diagnosis & Inspection Commands
```bash
export PATH="/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin:$PATH"

# 1. Pull message payload from DLQ without auto-acking
gcloud pubsub subscriptions pull recoveryos-workflow-execution-dlq-sub \
  --project=recoveryos-506713 \
  --limit=5
```

## 3. Decision Criteria
- If message payload contains malformed JSON or invalid schema version, trace the producer client.
- If message was rejected due to `tenant_mismatch` or `workflow_not_found`, identify the rogue caller.
- If message is a legitimate workflow that failed after 5 retry attempts, diagnose the permanent error.

## 4. Remediation Steps
- If poison pill is confirmed invalid and non-recoverable, acknowledge message to drain DLQ:
```bash
# Pull and acknowledge dead-letter message once inspected:
gcloud pubsub subscriptions pull recoveryos-workflow-execution-dlq-sub \
  --project=recoveryos-506713 \
  --auto-ack \
  --limit=1
```

## 5. Verification
- Verify DLQ subscription message count returns to 0.
- Ensure API client contract conforms to `WorkflowExecutionMessage` schema.

## 6. Escalation Condition
- More than 10 poison pill messages accumulate within 1 hour.
