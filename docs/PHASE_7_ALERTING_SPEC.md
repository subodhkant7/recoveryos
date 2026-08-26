# RecoveryOS Production Alerting Specification

This specification defines the standard alerting signals, evaluation windows, severity tiers, and runbook links for the RecoveryOS production environment.

---

## 1. Alerting Infrastructure Status

- **Prometheus Metrics Endpoint**: Implemented and active at `GET /metrics` on `recoveryos`.
- **Cloud Run Health & Readiness Probes**: Implemented and active at `GET /api/health` and `GET /api/ready`.
- **Structured Cloud Logging Events**: Implemented and active with canonical JSON payload schemas.
- **Alerting Rules & Notification Channels**: Specified below for Google Cloud Monitoring / Alerting Policies and Prometheus Alertmanager.

---

## 2. Production Alert Definitions

| Alert Name | Signal / Metric | Condition / Threshold | Duration | Severity | Likely Cause | Recommended Action & Runbook | Implementation Status |
|:---|:---|:---|:---:|:---:|:---|:---|:---:|
| **RecoveryOS_API_Elevated5xx** | `recoveryos_http_requests_total{status=~"5.."}` | $> 1\%$ of total request volume | 2 min | **PAGE** (P1) | Unhandled API exceptions, dependency outage, Firestore failure | Run `06_ELEVATED_API_ERRORS.md`; evaluate rollback | Specified |
| **RecoveryOS_ReadinessFailure** | Cloud Run readiness check or `/api/ready` | Response code $\neq 200$ | 1 min | **PAGE** (P1) | Firestore database unavailable, Secret Manager failure | Check Cloud Run logs; inspect Firestore database connectivity | Specified |
| **RecoveryOS_WorkerFailureSpike** | `recoveryos_worker_executions_total{status="nack"}` | $> 5$ consecutive NACKs or failure rate $> 5\%$ | 3 min | **PAGE** (P1) | Worker crash loops, persistent OCC mismatch, Firestore timeout | Run `01_WORKER_OUTAGE.md`; inspect worker service logs | Specified |
| **RecoveryOS_PubSubBacklogGrowth** | `pubsub.googleapis.com/subscription/num_undelivered_messages` | $> 50$ undelivered messages on `recoveryos-workflow-execution-worker` | 5 min | **WARN** (P2) | Worker instance scaling throttled, slow agent execution, network stalls | Run `02_PUBSUB_BACKLOG.md`; check Cloud Run worker max instances | Specified |
| **RecoveryOS_DLQMessagePresent** | `pubsub.googleapis.com/subscription/num_undelivered_messages` | $\ge 1$ message on `recoveryos-workflow-execution-dlq-sub` | 1 min | **WARN** (P2) | Poison pill, deserialization error, untrusted producer, tenant mismatch | Run `03_DLQ_GROWTH.md`; inspect DLQ payload and audit producer | Specified |
| **RecoveryOS_StuckWorkflowDetected** | `recoveryos_workflows_stuck_total` or diagnostics scanner | $\ge 1$ stuck workflow ($>300\text{s}$ in non-terminal state) | 5 min | **WARN** (P2) | Worker dropped claim without updating state; external dependency hung | Run `04_STUCK_WORKFLOW.md`; trigger `POST /api/workflows/{id}/recover` | Specified |
| **RecoveryOS_OCCConflictSpike** | `rate(recoveryos_occ_mismatches_total[5m])` | $> 0.2$ conflicts/sec | 5 min | **WARN** (P2) | Rapid concurrent redelivery of out-of-order events | Run `05_FIRESTORE_OCC_CONFLICT_SPIKE.md`; inspect event ordering | Specified |
| **RecoveryOS_HighDuplicateClaimRate** | `rate(recoveryos_duplicate_claims_total[5m])` | $> 20\%$ of total throughput | 5 min | **INFO** (P3) | Aggressive Pub/Sub redelivery, network re-transmits | Inspect Pub/Sub ack deadline settings (default: 60s) | Specified |
| **RecoveryOS_ElevatedRecoveries** | `rate(recoveryos_recoveries_total[10m])` | $> 5$ recoveries in 10 min | 10 min | **WARN** (P2) | Systematic workflow stalls requiring repeated operator intervention | Run `08_RECOVERY_REDRIVE.md`; analyze root cause of stalled workflows | Specified |
| **RecoveryOS_WorkerUnauthorizedAccess** | Cloud Run worker IAM 403 count | $> 5$ unauthorized attempts/min | 2 min | **WARN** (P2) | Probe from unauthorized source, misconfigured service account | Run `09_SECURITY_INCIDENT.md`; inspect source IPs and identities | Specified |

---

## 3. Prometheus Alerting Rules Configuration (PromQL)

```yaml
groups:
  - name: recoveryos_operational_alerts
    rules:
      - alert: RecoveryOS_API_Elevated5xx
        expr: (sum(rate(recoveryos_http_requests_total{status=~"5.."}[2m])) / sum(rate(recoveryos_http_requests_total[2m]))) > 0.01
        for: 2m
        labels:
          severity: critical
          service: recoveryos
        annotations:
          summary: "RecoveryOS API 5xx error rate exceeds 1%"
          runbook: "docs/runbooks/06_ELEVATED_API_ERRORS.md"

      - alert: RecoveryOS_WorkerFailureSpike
        expr: rate(recoveryos_worker_executions_total{status="nack"}[3m]) > 0.1
        for: 3m
        labels:
          severity: critical
          service: recoveryos-worker
        annotations:
          summary: "RecoveryOS Worker NACK rate is elevated"
          runbook: "docs/runbooks/01_WORKER_OUTAGE.md"

      - alert: RecoveryOS_OCCConflictSpike
        expr: rate(recoveryos_occ_mismatches_total[5m]) > 0.2
        for: 5m
        labels:
          severity: warning
          service: recoveryos-worker
        annotations:
          summary: "RecoveryOS OCC conflict rate exceeds 0.2/sec"
          runbook: "docs/runbooks/05_FIRESTORE_OCC_CONFLICT_SPIKE.md"
```
