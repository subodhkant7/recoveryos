# RecoveryOS Phase 6.5 — Operability, Observability & Recovery Runbook

---

## 1. Objective

Phase 6.5 transitions RecoveryOS from an experimentally tested system into an **operationally trustworthy production platform**. This phase standardizes:
1. End-to-end correlation tracing across asynchronous API $\rightarrow$ Pub/Sub $\rightarrow$ Worker hops.
2. Low-cardinality Prometheus metrics exposition on `/metrics`.
3. Stuck workflow detection via `GET /api/workflows/{workflow_id}/diagnostics`.
4. Role-gated, tenant-isolated, OCC-fenced operator recovery via `POST /api/workflows/{workflow_id}/recover`.
5. Runbook procedures for Dead-Letter Queue (DLQ) triage and safe redrive.

---

## 2. Existing Architecture

```
                                    +------------------------------------------+
                                    |         Cloud Run API Service            |
                                    |        (recoveryos-00006-jwt)            |
                                    +--------------------+---------------------+
                                                         |
                                      Pub/Sub Publish    |  POST /api/scenarios/*
                                      (fail-closed)      |  POST /api/workflows/*/recover
                                                         v
                                    +--------------------+---------------------+
                                    |          Cloud Pub/Sub Topic             |
                                    |    (recoveryos-workflow-execution)       |
                                    +----+-------------------------------+-----+
                                         |                               |
                   Push Subscription     |                               | Max 5 attempts
                   (OIDC Authenticated)  |                               | exhausted
                                         v                               v
                     +-------------------+-------------------+   +---------------+---------------+
                     |     Private Cloud Run Worker          |   |          Pub/Sub DLQ          |
                     |   (recoveryos-worker-00008-5pv)       |   | (recoveryos-workflow-execution|
                     |     - OperationClaim Lease (60s)      |   |            -dlq)              |
                     |     - OCC Version Check               |   +-------------------------------+
                     |     - Decision: ACK / NACK / 422      |
                     +-------------------+-------------------+
                                         |
                                         v
                     +-------------------+-------------------+
                     |         Firestore Database            |
                     |           (recoveryosdb)              |
                     |   - workflows / timeline events       |
                     |   - operation_claims / idempotency    |
                     +---------------------------------------+
```

---

## 3. Changes Implemented

- **Structured Event Taxonomy**: Standardized canonical lifecycle events (`WORKFLOW_DISPATCHED`, `WORKFLOW_CONSUMED`, `WORKFLOW_CLAIMED`, `WORKFLOW_DUPLICATE`, `WORKFLOW_OCC_MISMATCH`, `WORKFLOW_TERMINAL_SKIP`, `WORKFLOW_RECOVERED`, `WORKFLOW_ACK`, `WORKFLOW_NACK`, `WORKFLOW_DLQ`).
- **Prometheus Metrics Registry**: Added thread-safe low-cardinality metric counters to `/metrics`.
- **Stuck Workflow Diagnostics (`GET /api/workflows/{workflow_id}/diagnostics`)**: Analyzes workflow execution age, state progression, and lease expiration.
- **Safe Operator Recovery API (`POST /api/workflows/{workflow_id}/recover`)**: Role-gated (`OPERATOR`/`ADMIN`), tenant-isolated endpoint enforcing OCC version locking and immutable terminal state protection.

---

## 4. Observability Model & Correlation IDs

Every execution turn carries a consistent correlation tuple in structured JSON logs:

```json
{
  "timestamp": "2026-08-27T01:15:00.000000+00:00",
  "level": "INFO",
  "logger": "recoveryos.events.consumer",
  "service": "recoveryos",
  "environment": "production",
  "message": "Consuming workflow execution message",
  "request_id": "corr-rec-12345",
  "workflow_id": "74436ebe-c1cf-4d90-8250-35dfa1c0b567",
  "tenant_id": "tenant-acme",
  "extra": {
    "event_name": "WORKFLOW_CONSUMED",
    "message_id": "21444745455509067",
    "idempotency_key": "op_dispatch_74436ebe_v1",
    "expected_version": 1
  }
}
```

### Correlation Fields:
- `workflow_id`: Durable UUID of the business workflow in Firestore.
- `tenant_id`: Customer tenant namespace guaranteeing data isolation.
- `idempotency_key`: Deterministic token scoping the atomic operation claim.
- `message_id`: Google Cloud Pub/Sub message identifier.
- `expected_version`: Optimistic concurrency control (OCC) version token.
- `request_id` / `correlation_id`: End-to-end distributed trace identifier.

---

## 5. Operational Metrics

Exposed at `/metrics` (Prometheus text format):

| Metric | Type | Labels | Description |
| :--- | :---: | :--- | :--- |
| `recoveryos_workflows_dispatched_total` | Counter | `scenario`, `tenant_id` | Count of asynchronous workflow execution dispatches |
| `recoveryos_publish_failures_total` | Counter | `backend` | Count of failed Pub/Sub publish RPC attempts |
| `recoveryos_worker_executions_total` | Counter | `status`, `failure_type` | Delivery decisions (`ack`, `nack`, `dead_letter`) |
| `recoveryos_occ_mismatches_total` | Counter | None | Count of optimistic concurrency version collisions |
| `recoveryos_duplicate_claims_total` | Counter | None | Count of deduplicated redundant message deliveries |
| `recoveryos_recoveries_total` | Counter | `status` | Count of operator-initiated workflow recovery actions |

---

## 6. Safe DLQ & Recovery Procedures

> [!NOTE]
> **Delivery Semantics Invariant:**
> Google Cloud Pub/Sub provides **at-least-once delivery**. RecoveryOS enforces **idempotent/exactly-once business execution** through atomic Firestore `OperationClaim` leases and monotonic OCC version checks.

### When a Message Lands in DLQ:
1. **Diagnosis**: Query `recoveryos-workflow-execution-dlq-sub` or check Cloud Logging for `WORKER_INVALID_MESSAGE_SCHEMA` or `WORKER_CONSUMER_FATAL`.
2. **Root Cause Analysis**: Verify if the failure was a transient infrastructure bug or malformed payload.
3. **Safe Redrive / Recovery**:
   - An authorized `OPERATOR` or `ADMIN` calls `POST /api/workflows/{workflow_id}/recover`.
   - The API fetches current OCC version $V$, verifies tenant ownership, generates fresh idempotency key `op_recover_{workflow_id}_v{V}_{uuid}`, appends `WORKFLOW_RECOVERED` event to the workflow timeline, and publishes a new `RECOVERY_TRIGGER` event to Pub/Sub.
   - **Terminal Guard**: If the workflow is already `COMPLETED`, recovery is rejected (`HTTP 400`).

---

## 7. Stuck Workflow Diagnostics

Operators can inspect workflow health via:
```http
GET /api/workflows/{workflow_id}/diagnostics
Authorization: Bearer <JWT>
```

### Example Response:
```json
{
  "workflow_id": "74436ebe-c1cf-4d90-8250-35dfa1c0b567",
  "tenant_id": "tenant-acme",
  "state": "EXECUTING",
  "version": 2,
  "age_seconds": 245.5,
  "is_terminal": false,
  "is_stuck": true,
  "stuck_reason": "Active operation claim lease has expired without task completion.",
  "is_recoverable": true,
  "operation_claim": {
    "idempotency_key": "op_dispatch_74436ebe_v1",
    "status": "CLAIMED",
    "owner_worker_id": "recoveryos-worker-00008-5pv",
    "lease_expires_at": "2026-08-27T01:10:00.000000+00:00"
  },
  "event_count": 4,
  "last_event": {
    "event_type": "WORKFLOW_CLAIMED",
    "timestamp": "2026-08-27T01:09:00.000000+00:00"
  }
}
```

---

## 8. Rollback Procedure

If operational regressions occur:
1. Traffic can immediately be redirected to reserve revision `recoveryos-00004-sw7`:
   ```bash
   gcloud run services update-traffic recoveryos --region asia-east1 --to-revisions recoveryos-00004-sw7=100
   ```
2. Inspect worker logs:
   ```bash
   gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=recoveryos-worker" --limit 50 --format json
   ```
