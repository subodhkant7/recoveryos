# Runbook 07: Production Traffic Rollback Execution

## 1. Symptoms
- Severe production release regression, unhandled exceptions, or widespread service degradation following a revision cutover.

## 2. Diagnosis & Pre-Rollback Checks
```bash
export PATH="/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin:$PATH"

# 1. Verify rollback reserve revision exists and is Ready
gcloud run revisions describe recoveryos-00006-jwt \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --format="value(status.conditions[0].status)"
```

## 3. Rollback Execution Command
```bash
# Execute immediate atomic traffic shift to rollback revision:
gcloud run services update-traffic recoveryos \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --to-revisions=recoveryos-00006-jwt=100
```

## 4. Immediate Post-Rollback Verification
```bash
# 1. Confirm traffic allocation
gcloud run services describe recoveryos \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --format="json(status.traffic)"

# 2. Check health and readiness probes
python3 -c "
import subprocess, httpx
id_token = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()
headers = {'X-Serverless-Authorization': f'Bearer {id_token}'}
r = httpx.get('https://recoveryos-321161003794.asia-east1.run.app/api/health', headers=headers)
assert r.status_code == 200
print('Rollback revision health verified: 200 OK')
"
```

## 5. Escalation Condition
- Rollback target revision fails to serve traffic or encounters readiness failure.
