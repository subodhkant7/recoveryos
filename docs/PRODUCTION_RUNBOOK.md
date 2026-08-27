# RecoveryOS Production Operations Runbook

**Environment**: Production (`recoveryos-506713` / `asia-east1`)  
**Active Services**:
- API: `recoveryos` (`https://recoveryos-321161003794.asia-east1.run.app`) -> Active Revision: `recoveryos-00019-vog`
- Worker: `recoveryos-worker` (`https://recoveryos-worker-321161003794.asia-east1.run.app`) -> Active Revision: `recoveryos-worker-00009-829`
- Pub/Sub Topic: `recoveryos-workflow-execution`
- Worker Push Subscription: `recoveryos-workflow-execution-worker`
- DLQ Topic / Subscription: `recoveryos-workflow-execution-dlq` / `recoveryos-workflow-execution-dlq-sub`
- Database: Firestore `recoveryosdb`
- Cloud Monitoring Dashboard: `projects/321161003794/dashboards/b850c147-9c77-4b9a-a910-f121312eee23`

---

## 1. Emergency API Rollback Procedure

If the production API exhibits sustained 5xx errors, latency anomalies, or security regressions:
```bash
./google-cloud-sdk/bin/gcloud run services update-traffic recoveryos \
  --to-revisions=recoveryos-00008-2bt=100 \
  --region=asia-east1 \
  --project=recoveryos-506713
```
- **Expected Recovery Time**: < 5 seconds.
- **Verification**: `curl -s https://recoveryos-321161003794.asia-east1.run.app/api/health`

---

## 2. Emergency Worker Rollback Procedure

If the asynchronous worker experiences unhandled exceptions, lease heartbeat failures, or push processing bottlenecks:
```bash
./google-cloud-sdk/bin/gcloud run services update-traffic recoveryos-worker \
  --to-revisions=recoveryos-worker-00008-5pv=100 \
  --region=asia-east1 \
  --project=recoveryos-506713
```
- **Expected Recovery Time**: < 5 seconds.
- **Verification**: `curl -s -H "Authorization: Bearer $(./google-cloud-sdk/bin/gcloud auth print-identity-token)" https://recoveryos-worker-321161003794.asia-east1.run.app/api/health`

---

## 3. Worker Outage & Restart Investigation

1. **Check Worker Logs**:
   ```bash
   ./google-cloud-sdk/bin/gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="recoveryos-worker" AND severity>=ERROR' --limit=20 --project=recoveryos-506713
   ```
2. **Check Cloud Run Revision Status**:
   ```bash
   ./google-cloud-sdk/bin/gcloud run services describe recoveryos-worker --region=asia-east1 --project=recoveryos-506713
   ```

---

## 4. Pub/Sub Backlog & Push Bottleneck Investigation

1. **Check Subscription Backlog**:
   ```bash
   ./google-cloud-sdk/bin/gcloud pubsub subscriptions describe recoveryos-workflow-execution-worker --project=recoveryos-506713
   ```
2. **Inspect Push Delivery Failures**:
   - Verify OIDC Token Service Account has `roles/run.invoker` on `recoveryos-worker`.
   - Verify worker concurrency setting (`concurrency: 10`).

---

## 5. Dead-Letter Queue (DLQ) Poison Message Remediation

1. **Pull and Inspect Dead-Lettered Messages**:
   ```bash
   ./google-cloud-sdk/bin/gcloud pubsub subscriptions pull recoveryos-workflow-execution-dlq-sub --auto-ack=false --limit=5 --project=recoveryos-506713
   ```
2. **Identify Malformed Payload or Failure Cause**.
3. **Acknowledge or Replay**: Once fixed, acknowledge from DLQ and re-dispatch workflow via `/api/scenarios/{name}`.

---

## 6. Firestore Database & OCC Conflict Remediation

1. **Verify Database Connectivity**: Ensure Cloud Run instances have access to database `recoveryosdb`.
2. **Stuck Workflow Detection**:
   - Query `/api/workflows/{workflow_id}/diagnostics` to check `is_stuck` flag.
   - Force recovery transition via `POST /api/workflows/{workflow_id}/recover` if necessary.

---

## 7. SSE Disconnection & Ticket Replay Troubleshooting

1. **Ticket Protocol**: Clients must request single-use tickets from `/api/auth/sse-ticket` (60s TTL).
2. **Replay Defense**: Reusing a consumed ticket will return HTTP 401. If disconnected, the UI client must request a new ticket before establishing a new event stream.

---

## 8. Incident Investigation & Log Queries

- **Search All Production 5xx Errors**:
  ```bash
  ./google-cloud-sdk/bin/gcloud logging read 'resource.type="cloud_run_revision" AND (resource.labels.service_name="recoveryos" OR resource.labels.service_name="recoveryos-worker") AND httpRequest.status>=500' --limit=50 --project=recoveryos-506713
  ```
- **Trace Specific Workflow ID**:
  ```bash
  ./google-cloud-sdk/bin/gcloud logging read 'jsonPayload.workflow_id="<WORKFLOW_ID>"' --project=recoveryos-506713
  ```

---

## 9. Secret Rotation Procedure

1. **Update Secret in Secret Manager**:
   ```bash
   echo -n "NEW_SECRET_VALUE" | ./google-cloud-sdk/bin/gcloud secrets versions add recoveryos-jwt-secret --data-file=- --project=recoveryos-506713
   ```
2. **Deploy Revision to Mount `:latest`**:
   Deploy a new revision or restart instances to pick up the new secret version.
