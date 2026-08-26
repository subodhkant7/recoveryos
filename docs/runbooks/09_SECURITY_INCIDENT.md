# Runbook 09: Security Incident & Unauthorized Access Response

## 1. Symptoms
- Spike in HTTP 401 Unauthorized or HTTP 403 Forbidden responses.
- Repeated cross-tenant access attempts in Cloud Logging (`EVENT_TENANT_MISMATCH`).
- Unauthorized requests at the private worker edge.

## 2. Diagnosis & Inspection Commands
```bash
export PATH="/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin:$PATH"

# 1. Query security-related event logs
gcloud logging read 'jsonPayload.event_name=~"EVENT_TENANT_MISMATCH|WORKER_SECURITY_DENIED"' \
  --project=recoveryos-506713 \
  --limit=25 \
  --format="json(timestamp,jsonPayload.workflow_id,jsonPayload.expected_tenant,jsonPayload.message_tenant,textPayload)"

# 2. Inspect IAM binding for public access leaks on worker
gcloud run services get-iam-policy recoveryos-worker \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --format="json(bindings)"
```

## 3. Decision Criteria
- If `allUsers` is present in worker IAM policy: Immediate critical vulnerability. Remove `allUsers` binding instantly.
- If repeated cross-tenant requests originate from a specific JWT `sub` (principal): Revoke or rotate the JWT secret.
- If expired or invalid signatures are encountered: Verify client token generation clocks.

## 4. Remediation Steps
```bash
# Ensure worker has ZERO public invoker roles:
gcloud run services remove-iam-policy-binding recoveryos-worker \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --member="allUsers" \
  --role="roles/run.invoker"
```

## 5. Verification
- Verify unauthenticated probe to worker URL returns `HTTP 403 Forbidden`.
- Confirm cross-tenant reads return `HTTP 403 Forbidden`.

## 6. Escalation Condition
- Confirmed unauthorized data exfiltration or compromised service account credentials.
