# Phase 6.2: Distributed Asynchronous Execution Architecture

---

## 1. Executive Summary & Objective
Phase 6.2 designs the distributed asynchronous architecture for RecoveryOS. It decouples synchronous API request ingestion from autonomous multi-agent execution, introduces Google Cloud Pub/Sub with dead-letter routing, establishes a dedicated worker service contract, preserves deterministic effectively-once mutation invariants, and designs multi-replica Gemini quota coordination.

---

## 2. Architectural Comparison: Gemini Quota Coordination Options

| Metric / Dimension | Option 1: Redis-Based Distributed Token Bucket | Option 2: Cloud Tasks Push Rate Limiter | Option 3: Pub/Sub Worker Concurrency Control | Option 4: Firestore Leased Window Token Bucket |
| :--- | :--- | :--- | :--- | :--- |
| **Correctness & Precision** | **Very High** (Sub-millisecond sliding window token bucket via Redis Lua scripts) | **High** (Configurable `max_dispatches_per_second` on Cloud Tasks queue) | **Medium** (Coarse-grained; worker concurrency limits do not bound LLM requests per minute) | **High** (Atomic Firestore transactions with leased timestamp tokens) |
| **Implementation Complexity** | Medium (Requires Redis connection pool, Lua script, fallback) | Medium (Requires Cloud Tasks client and push handler endpoint) | Low (Configured purely in Pub/Sub subscription settings) | **Low-Medium** (Uses existing Firestore transactional store) |
| **Operational Burden** | **High** (Requires VPC Connector, Memorystore instance, maintenance windows, monitoring) | **Low** (Fully managed serverless GCP service) | **Very Low** (Fully managed serverless Pub/Sub) | **Very Low** (Zero new infrastructure; uses existing Firestore) |
| **Latency Overhead** | $< 2\text{ms}$ | $10\text{ms} - 50\text{ms}$ | $20\text{ms} - 100\text{ms}$ | $15\text{ms} - 40\text{ms}$ |
| **Failure Modes** | Redis connection drop / cold restart / memory leak | Cloud Tasks queue pause / queue quota limits | Worker starvation under message burst | Firestore contention under high write contention |
| **Serverless Compatibility** | Requires Serverless VPC Access connector on Cloud Run | Native Cloud Run IAM integration | Native Cloud Run Push/Pull integration | Native Cloud Run ADC integration |
| **GCP Cost Overhead** | **High** (~$35–$50/month for minimal Memorystore instance + VPC connector) | **Very Low** (Free tier: 1M tasks/month; $0.40/million thereafter) | **Zero** (Included in Pub/Sub free tier) | **Zero** (Included in Firestore free tier) |
| **Suitability for Gemini 15 RPM** | **Excellent** (Enforces exact token bucket across $N$ replicas) | **Excellent** (Queue dispatch rate bounds aggregate calls) | **Poor** (Cannot prevent burst LLM calls within an active turn) | **Very Good** (Enforces exact token bucket via leased OCC token document) |

### **Architectural Recommendation: Cloud Tasks + Firestore Leased Window (Two-Tier Coordination)**
1. **Primary Queue Tier (Cloud Tasks):** Cloud Tasks queue `recoveryos-gemini-queue` configured with `max_dispatches_per_second: 0.25` (15 RPM maximum).
2. **Deterministic Safety Tier (Firestore Leased Window):** In the worker process, `ResilientGemini` validates against a lightweight `/system/gemini_quota_lease` Firestore document using atomic transactions before executing the turn.
3. **Rationale:** Eliminates the ~$50/month Memorystore VPC overhead, eliminates operational database management, maintains 100% serverless scale-to-zero, and mathematically guarantees zero quota breaches across multi-instance worker fleets.

---

## 3. Detailed Architectural Specification (Sections A–T)

### A. Current Architecture (Phase 6.1.1 Baseline)
- **Monolithic Ingestion & Execution:** Single Cloud Run instance (`maxScale: 1`).
- **Synchronous Agent Trigger:** `POST /api/scenarios/{name}` creates workflow in Firestore, then immediately spawns an in-process `asyncio.create_task(_run_agent(workflow_id))`.
- **Limitation:** HTTP connection and container lifecycle are coupled to long-running Gemini agent turns. If the container crashes or restarts during agent execution, the workflow remains in `EXECUTING` until restarted.

### B. Target Architecture (Phase 6.2)
```
+----------------------------------------------------------------------------------------------------+
|                                    Target Distributed Topology                                     |
+----------------------------------------------------------------------------------------------------+

     [ Client / Webhook ] 
             |
             v (JWT Authenticated HTTPS)
  +------------------------------------------------------+
  | API Service (Cloud Run - Ingestion Fleet)            |
  | - Validates JWT & RBAC                               |
  | - Enforces Tenant Isolation                          |
  | - Writes CREATED Workflow to Firestore (OCC)         |
  | - Publishes WORKFLOW_DISPATCH event to Pub/Sub       |
  | - Returns 202 Accepted (< 50ms)                      |
  +------------------------------------------------------+
             |
             v
  +------------------------------------------------------+
  | Google Cloud Pub/Sub Topic: recoveryos-workflow-events|
  +------------------------------------------------------+
             |
             | (At-Least-Once Delivery / Push or Pull)
             v
  +------------------------------------------------------+
  | Worker Service Fleet (Cloud Run Worker / Scaling N)  |
  | - Consumes WORKFLOW_DISPATCH / APPROVAL_RESUME       |
  | - Validates Message Schema & Tenant Authenticity     |
  | - Checks Firestore OCC & OperationClaim Lease        |
  | - Executes Autonomous Gemini Multi-Agent Turns       |
  | - Coordinates Gemini RPM via Cloud Tasks/Firestore   |
  | - Executes Deterministic Tools & Outcome Contracts   |
  | - Commits Completion & Emits Audit Events            |
  | - ACKs Pub/Sub Message upon Step/Terminal Completion |
  +------------------------------------------------------+
             |
             v
  +------------------------------------------------------+
  | Google Cloud Firestore (Primary Persistent Datastore) |
  | - Workflows, Steps, Events, Claims, Idempotency, OCC  |
  +------------------------------------------------------+
```

### C. API Service Responsibilities
1. Authenticate callers via JWT (HMAC-SHA256).
2. Authorize actions via role-based access control (`VIEWER`, `OPERATOR`, `APPROVER`, `ADMIN`).
3. Enforce tenant isolation boundaries (`principal.tenant_id`).
4. Perform atomic workflow initialization in Firestore (`state: CREATED`, `version: 1`).
5. Publish message to Pub/Sub topic `recoveryos-workflow-events` with correlation context.
6. Return `HTTP 202 Accepted` with `workflow_id` and poll URL immediately.

### D. Pub/Sub Topic & Subscription Design
- **Topic:** `recoveryos-workflow-events`
- **Subscription:** `recoveryos-worker-sub` (Dead-letter enabled, `ack_deadline: 60s`, `max_delivery_attempts: 5`).
- **Dead-Letter Topic:** `recoveryos-workflow-deadletter`
- **Dead-Letter Subscription:** `recoveryos-deadletter-sub`

### E. Worker Service Responsibilities
1. Ingest Pub/Sub messages with schema validation (`WorkflowExecutionMessage`).
2. Deduplicate message IDs via Firestore idempotency records.
3. Acquire operation lease via `OperationClaim` (60s TTL).
4. Fetch current workflow snapshot from Firestore with OCC version check.
5. Invoke `Taskmaster` and `RecoverySpecialist` via `ResilientGemini`.
6. Enforce deterministic policy engine and outcome contracts before committing mutations.
7. Update workflow state, steps, and timeline in Firestore with version increment.
8. ACK message on success; NACK on retryable failure; route to dead-letter on terminal unrecoverable error.

### F. Message Schema
Defined in detail in [artifacts/phase6/phase6_2_message_contract.md](file:///Users/urjasoft/Documents/Recovery%20OS/artifacts/phase6/phase6_2_message_contract.md).

### G. Idempotency & Effectively-Once Effect Strategy
- **Message Deduplication Key:** `msg_claim_{message_id}` stored in Firestore with 24h TTL.
- **Tool Operation Key:** `op_{tool_name}_{workflow_id}_{target_id}_{param_hash}`.
- If a message is redelivered while an operation is in progress by another worker, the second worker observes the active claim lease and awaits or ignores the duplicate.
- If a message is redelivered after an operation has completed, the worker fetches the cached result without re-executing external mutations.

### H. Firestore Transaction & OCC Interaction
- Every state transition uses `@firestore.async_transactional` to verify `current_ver == expected_version`.
- If an OCC collision occurs (`StaleWorkflowStateError`), the worker drops the stale redelivery or re-fetches the latest snapshot.

### I. Retry Strategy
- **Retryable Errors:** Gemini 429 (`RESOURCE_EXHAUSTED`), Gemini 503 (`UNAVAILABLE`), Transient Firestore network disconnects.
- **Backoff:** Exponential backoff with full jitter ($2\text{s}, 4\text{s}, 8\text{s}, 16\text{s}, 30\text{s}$).
- **Subscription NACK:** Worker delays NACK to allow Cloud Pub/Sub exponential backoff.

### J. Dead-Letter & Poison-Message Strategy
- After 5 unsuccessful delivery attempts, Pub/Sub forwards the message to `recoveryos-workflow-deadletter`.
- The worker updates the target workflow in Firestore to `WorkflowState.ESCALATED` with reason `"Max retry attempts exceeded; forwarded to dead-letter"`.
- A Cloud Monitoring alert triggers an operator notification.

### K. Worker Crash Recovery
- If a worker dies mid-execution:
  1. The operation claim lease expires after 60 seconds.
  2. Pub/Sub re-delivers the unacknowledged message.
  3. The replacement worker inspects external provider state via `reconcile_interrupted_workflow`, verifies existing evidence, and resumes execution seamlessly.

### L. Distributed Gemini Quota Coordination
- **Queue Layer:** Cloud Tasks queue `recoveryos-gemini-queue` configured with `max_dispatches_per_second: 0.25` (15 RPM maximum).
- **Safety Fallback:** Firestore token document `/system/gemini_quota_lease` updated via atomic transaction with minimum 6.5s interval timestamp before executing turns across any worker instance.

### M. Scaling Strategy
- **API Service:** Auto-scales freely ($0 \rightarrow N$) based on HTTP request volume.
- **Worker Service:** Auto-scales ($0 \rightarrow N$) based on Pub/Sub subscription queue depth, constrained by Cloud Tasks dispatch rate to protect Gemini quota.

### N. Authentication & Security Boundaries
- API requests require JWT signed with `JWT_SECRET_KEY` from Secret Manager.
- Worker service is private, invocable only by Cloud Pub/Sub push subscription via Google Cloud Service Account IAM (`roles/run.invoker`).

### O. Tenant Isolation
- `tenant_id` is embedded in the signed message payload.
- Worker validates that all tool executions and Firestore queries are scoped strictly to `tenant_id`.

### P. Observability & Distributed Tracing
- `X-Request-ID` and OpenTelemetry `traceparent` propagate across API $\rightarrow$ Pub/Sub message attributes $\rightarrow$ Worker contextvars $\rightarrow$ Firestore audit events $\rightarrow$ Prometheus metrics.
- All logs emitted as structured JSON with automatic secret redaction.

### Q. Deployment Topology
- **Production Service 1:** `recoveryos-api` (Public/Edge IAM HTTPS service).
- **Production Service 2:** `recoveryos-worker` (Internal Cloud Run push subscriber).
- **Managed Resources:** Cloud Pub/Sub topic & subscriptions, Firestore database, Secret Manager secrets, Cloud Tasks queue.

### R. Rollback Strategy
- If a worker deployment fails, revert traffic split to previous revision `recoveryos-00004-sw7` instantly via `gcloud run services update-traffic`.
- Unconsumed Pub/Sub messages remain safely buffered in the topic.

### S. Cost Implications
- **Cloud Pub/Sub:** Free tier covers 10 GB/month ($0.00).
- **Cloud Tasks:** Free tier covers 1M operations/month ($0.00).
- **Firestore:** Free tier covers 50K reads, 20K writes/day ($0.00).
- **Cloud Run:** Free tier covers 2M requests, 360K vCPU-seconds ($0.00–$5.00/month estimated).
- **Total Expected Monthly Cost:** **$0.00 – $5.00 / month**.
