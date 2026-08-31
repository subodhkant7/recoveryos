# Runbook 01: Worker Outage & Crash-Loop Recovery

## 1. Symptoms
- Pub/Sub messages are not advancing to Firestore `EXECUTING` state.
- `recoveryos_worker_executions_total{status="nack"}` metric is spiking.
- Worker Cloud Run service returns HTTP 500 or times out on push delivery.

## 2. Diagnosis & Inspection Commands
```bash
# 1. Check worker service status and active revision
gcloud run services describe recoveryos-worker \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --format="json(status.latestReadyRevisionName,status.conditions)"

# 2. Check recent worker error logs
gcloud logging read 'resource.labels.service_name="recoveryos-worker" AND severity>=ERROR' \
  --project=recoveryos-506713 \
  --limit=20 \
  --format="json(timestamp,jsonPayload.message,textPayload)"
```

## 3. Decision Criteria
- If logs show container memory limit exceeded (`Memory limit of 512Mi exceeded`), increase memory allocation.
- If logs show unhandled exception during startup or message processing, verify revision integrity.
- If worker revision is not `Ready=True`, redeploy or rollback worker.

## 4. Remediation Steps
```bash
# To restart or adjust worker container resources:
gcloud run services update recoveryos-worker \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --memory=1Gi
```

## 5. Verification
```bash
# Verify worker health probe (via authenticated OIDC token)
python3 -c "
import subprocess, httpx
token = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()
r = httpx.get('https://recoveryos-worker-321161003794.asia-east1.run.app/api/health', headers={'X-Serverless-Authorization': f'Bearer {token}'})
assert r.status_code == 200, f'Worker health check failed: {r.status_code}'
print('Worker healthy: 200 OK')
"
```

## 6. Escalation Condition
- If worker continues crash-looping for $> 5$ minutes, escalate to Lead Infrastructure Engineer.
