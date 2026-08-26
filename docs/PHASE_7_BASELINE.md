# RecoveryOS Phase 7 Operational Baseline

## 1. Executive Baseline Architecture

RecoveryOS is an asynchronous, event-driven recovery management system deployed to Google Cloud Platform.

```
Client (Web / Agent / API)
          │  HTTPS / OIDC Token + App JWT
          ▼
Cloud Run API: `recoveryos` (`recoveryos-00008-2bt`)
  - Endpoints: `/api/scenarios/*`, `/api/workflows/*`, `/api/health`, `/api/ready`, `/metrics`
  - Ingress: Public Edge (Protected by Cloud Run IAM + App RBAC)
          │
          │  Publish `WorkflowExecutionMessage`
          ▼
Google Cloud Pub/Sub Topic: `recoveryos-workflow-execution`
  - At-least-once message delivery guarantee
  - Dead Letter Queue: `recoveryos-workflow-execution-dlq` (Max delivery attempts: 5)
          │
          │  Authenticated OIDC Push (`X-Serverless-Authorization`)
          ▼
Cloud Run Worker: `recoveryos-worker` (`recoveryos-worker-00008-5pv`)
  - Ingress: Private Edge (Zero public ingress / HTTP 403 unauthenticated)
  - Role: Message deserialization, provenance verification, distributed deduplication
          │
          │  Transactional Leases (`OperationClaim`) & OCC Transitions
          ▼
Google Cloud Firestore: Database `recoveryosdb`
  - Collections: `workflows`, `events`, `idempotency`, `gemini_quota_leases`
```

---

## 2. Verified Operational Guarantees

| Subsystem | Guarantee | Mechanism |
|:---|:---|:---|
| **Message Transport** | At-least-once delivery | Google Cloud Pub/Sub with push subscription and dead-letter topics |
| **Business Execution** | Exactly-once business state transition | `OperationClaim` distributed lease fencing + Firestore OCC version validation |
| **State Persistence** | Monotonically increasing versioning | `version` check ($V_{\text{new}} = V_{\text{expected}} + 1$) inside Firestore transactions |
| **Edge Security** | Double-envelope authentication | Google Cloud IAM OIDC token (ingress) + RecoveryOS JWT bearer token (app RBAC) |
| **Tenant Isolation** | Strict data segregation | Every read, write, claim, and diagnostic operation validates `tenant_id` matching |
| **Worker Privacy** | Zero public access | Worker service rejects unauthenticated requests with `HTTP 403 Forbidden` |
| **Resilience & Safe Fallback** | Deterministic error routing | Transient errors trigger `NACK` (Pub/Sub retry); permanent errors trigger `DEAD_LETTER` |

---

## 3. Existing Operational Protections

1. **Distributed Idempotency (`OperationClaim`)**:
   - Every Pub/Sub event carries a deterministic `idempotency_key` (`op_dispatch_{workflow_id}`).
   - Claims are acquired atomically in Firestore. Replayed or duplicate messages encounter existing claims and return `SKIPPED_DUPLICATE` without re-executing state mutations.
2. **Optimistic Concurrency Control (OCC)**:
   - Workflows maintain a monotonically incrementing `version` integer.
   - Workers verify `expected_version == workflow.version` before advancing state. Concurrent or stale executions raise `StaleWorkflowStateError` and trigger redelivery without corrupting state.
3. **Structured Correlation Tracing**:
   - `correlation_id` / `request_id` propagates from API headers across Pub/Sub attributes, Worker contextvars, and Firestore timeline events.
4. **Zero-Cardinality Metric Instrumentation**:
   - Prometheus `/metrics` exporter records low-cardinality counters (`recoveryos_workflows_dispatched_total`, `recoveryos_worker_executions_total`, `recoveryos_occ_mismatches_total`, `recoveryos_duplicate_claims_total`, `recoveryos_recoveries_total`).
5. **Diagnostics & Stuck Workflow Detection**:
   - `GET /api/workflows/{workflow_id}/diagnostics` computes runtime age, pending claims, and determines if a workflow is stuck ($>300\text{s}$ in non-terminal state without active worker lease).
6. **Fenced Operator Recovery**:
   - `POST /api/workflows/{workflow_id}/recover` validates operator RBAC, enforces tenant isolation, rejects terminal workflows, and dispatches redrive events with fresh monotonic version expectations.

---

## 4. Operational Gaps Identified for Phase 7 Hardening

1. **Failure Mode Documentation**: Complete matrix mapping every failure mode to automated detection, system response, and operator action.
2. **Operational Runbooks**: Standardized, production-ready runbooks for worker outages, Pub/Sub backlogs, DLQ growth, stuck workflows, and OCC spikes.
3. **Alerting Specification**: Formal alert thresholds and SLO definitions for Cloud Monitoring and Prometheus.
4. **Capacity & Degradation Envelope**: Explicit documentation of scaling limits, memory/CPU bounds, concurrency ceilings, and graceful degradation paths.
5. **Comprehensive Operational Test Suite**: Regression suite verifying all corner cases (duplicate delivery races, worker crashes before/after claim, poison pills, cross-tenant violations).

---

## 5. Scope Boundaries: Explicitly Unchanged Items

To maintain production stability on `recoveryos-00008-2bt`:
- **NO redesign of the Pub/Sub $\rightarrow$ Worker architecture.**
- **NO modification of Firestore database name (`recoveryosdb`) or core schema.**
- **NO disruption of production traffic (maintained at 100% on `recoveryos-00008-2bt`).**
- **NO rotation of production secrets or IAM role restructuring.**
- **NO replacement of existing libraries with speculative abstractions.**
