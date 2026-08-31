# Runbook 06: Elevated API Errors (5xx)

## 1. Symptoms
- API response codes show $> 1\%$ HTTP 500 / 502 / 503 / 504 errors.
- Alert `RecoveryOS_API_Elevated5xx` triggers.

## 2. Diagnosis & Inspection Commands
```bash
# 1. Read recent API error logs
gcloud logging read 'resource.labels.service_name="recoveryos" AND severity>=ERROR' \
  --project=recoveryos-506713 \
  --limit=25 \
  --format="json(timestamp,jsonPayload.message,textPayload)"

# 2. Check service readiness and Firestore database health
python3 -c "
import subprocess, httpx
id_token = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()
r = httpx.get('https://recoveryos-321161003794.asia-east1.run.app/api/ready', headers={'X-Serverless-Authorization': f'Bearer {id_token}'})
print('Readiness Probe:', r.status_code, r.json())
"
```

## 3. Decision Criteria
- If readiness probe returns 503: Firestore or Secret Manager connectivity issue.
- If errors correlate with a recent deployment: Initiate immediate traffic rollback.
- If errors are isolated to a single scenario endpoint: Check scenario parameter validation.

## 4. Remediation Steps
- If deployment regression: Execute rollback to `recoveryos-00006-jwt` (see `07_ROLLBACK.md`).
- If transient Firestore failure: Check Google Cloud status dashboard.

## 5. Verification
- Verify `GET /api/health` and `GET /api/ready` return 200 OK.
- Monitor error rate until 5xx percentage drops below $0.1\%$.

## 6. Escalation Condition
- API outage $> 3$ minutes with readiness failures.
