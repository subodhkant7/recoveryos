# Runbook 10: Production Degradation & Throttling Triage

## 1. Symptoms
- Elevated p95 API latency ($> 500\text{ms}$).
- Pub/Sub message delivery delays.
- Worker processing backlog.

## 2. Diagnosis & Inspection Commands
```bash
export PATH="/Users/urjasoft/Documents/Recovery OS/google-cloud-sdk/bin:$PATH"

# 1. Check Cloud Run CPU & Memory utilization metrics
gcloud run services describe recoveryos \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --format="json(spec.template.spec.containers[0].resources)"

# 2. Check Prometheus metrics for request duration
python3 -c "
import subprocess, httpx
id_token = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()
r = httpx.get('https://recoveryos-321161003794.asia-east1.run.app/metrics', headers={'X-Serverless-Authorization': f'Bearer {id_token}'})
for line in r.text.splitlines():
    if 'duration' in line or 'requests_total' in line:
        print(line)
"
```

## 3. Decision Criteria
- If latency is driven by high concurrent connections exceeding container concurrency: Increase `max-instances`.
- If latency is driven by external Gemini rate limits: Allow circuit breaker cooldown (6.5s spacing).
- If Firestore latency is elevated: Check GCP status dashboard for regional Firestore degradation.

## 4. Remediation Steps
```bash
# Temporarily increase instance limits to absorb traffic surge:
gcloud run services update recoveryos \
  --project=recoveryos-506713 \
  --region=asia-east1 \
  --max-instances=3
```

## 5. Verification
- Verify p95 request duration drops back below $100\text{ms}$.
- Confirm no dropped connections or 5xx spikes.
