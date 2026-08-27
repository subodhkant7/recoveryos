# RecoveryOS Phase 10 Proposal: Full-Lifecycle Asynchronous Worker Execution & Production Control Plane

**Target System**: RecoveryOS (`recoveryos-506713`)  
**Status**: PROPOSAL & ARCHITECTURAL SPECIFICATION FOR HUMAN REVIEW  
**Author**: RecoveryOS Core Architecture & Systems Engineering Team  
**Preceding Milestone**: Phase 9 Operator Control Plane & Recovery Console (`dd416b8b`)  
**Baseline Test Count**: 317 tests collected (287 passed, 15 skipped, 0 failures)  
**Production Status**: 100% UNTOUCHED (`recoveryos-00008-2bt` @ 100% traffic)

---

## 1. Executive Summary

Phases 1 through 8 established an exceptional reliability foundation: Optimistic Concurrency Control (OCC), distributed lease operation claims, fail-closed Pub/Sub $\leftrightarrow$ Worker error classification, and 20 verified resilience scenarios across 307 automated tests. Phase 9 introduced the Operator Control Plane backend and web console.

A forensic audit of the post-Phase 9 codebase revealed critical architectural realities:
1. **The Asynchronous Worker Execution Loop is Incomplete**: The Cloud Run Worker (`backend/worker/service.py` $\rightarrow$ `backend/events/consumer.py`) validates messages, acquires leases, and transitions workflows to `EXECUTING`, but **does not actually invoke `AgentFactory` / `Taskmaster` / Gemini LLM** to execute tool steps to outcome verification and `COMPLETED`. Asynchronous execution in production is currently a stub state transition.
2. **The Operator Console is a Development Prototype**: The web UI at `/console` relies on client-side mock JWTs (`mock_dev_signature`), which will be immediately rejected with HTTP 401 in production mode. Furthermore, browser `EventSource` cannot send `Authorization` headers, breaking SSE streams.
3. **Database Query Scalability Bottlenecks**: `FirestoreWorkflowStore.list_workflows` and `count_workflows` download the **entire Firestore collection into memory** on every request before slicing in Python. No composite Firestore index specification (`firestore.indexes.json`) exists.
4. **SSE Firestore Polling Pressure**: `/api/workflows/{id}/events/stream` executes tight 1-second Firestore polling loops (`get_events` + `get_workflow`) per connected client.

**Phase 10 Objective**:
Bridge the execution gap and productionize the platform by delivering **Full-Lifecycle Asynchronous Worker Execution, Production Operator Session Authentication, and Scalable Cursor-Based Persistence**.

---

## 2. Forensic Repository Audit Matrix

| Subsystem / Capability | Genuine Code Implementation | Automated Test Coverage | Production Readiness | Evidence in Codebase | Remaining Architectural Gap |
|:---|:---:|:---:|:---:|:---|:---|
| **Distributed Operation Claims & Leases** | **YES** | **YES** (Phases 6–8) | **READY** | `workflow_store.py: claim_operation, complete_operation` | 60s lease works reliably. |
| **OCC Concurrency Fencing ($V \rightarrow V+1$)** | **YES** | **YES** | **READY** | `workflow_store.py: save_workflow(expected_version)` | Strict version check enforced. |
| **Pub/Sub $\rightarrow$ Worker Ingress & DLQ Routing** | **YES** | **YES** | **READY** | `worker/server.py: /`, `worker/service.py` | Maps 200=ACK, 500=NACK, 422=DLQ. |
| **Asynchronous Worker Agent Execution** | **NO (STUB)** | **MOCKED** | **NOT READY** | `events/consumer.py: lines 226-256` | **CRITICAL GAP**: Worker transitions state to `EXECUTING` but never runs `AgentFactory` or tool steps! |
| **Local In-Process Agent Execution** | **YES** | **YES** | **LOCAL ONLY** | `api/server.py: _run_agent` | Only runs in local in-memory dispatch mode. |
| **Security & RBAC Enforcement** | **YES** | **YES** | **READY** | `security/dependencies.py`, `security/principal.py` | Enforces `Role`, `tenant_id`, and `Permission`. |
| **Operator API Endpoints (Phase 9)** | **YES** | **YES** (10 tests) | **NEAR READY** | `api/server.py: /api/operator/*, /api/audit/*` | Filtering, overview, stuck aggregation, cancellation work. |
| **Operator Console Web UI (`/console`)** | **YES** | **PARTIAL** | **PROTOTYPE** | `api/static/app.js`, `index.html` | Client-side mock JWTs; no real production auth session; SSE lacks auth header support. |
| **Firestore Query Efficiency & Indexes** | **NO** | **NO** | **UNSCALABLE** | `persistence/workflow_store.py: list_workflows` | Streams entire collection into memory; lacks Firestore composite index definitions. |
| **Real-Time Event Streaming (SSE)** | **PARTIAL** | **YES** | **UNSCALABLE** | `api/server.py: stream_events` | 1s Firestore polling per connection; no header auth support in native EventSource. |
| **Persistent Security Audit Trail** | **YES** | **YES** | **READY** | `models/audit.py`, `persistence/workflow_store.py` | Stored in `audit_events` collection with actor, tenant, action, and reason. |
| **Dead-Letter Queue (DLQ) Management** | **PARTIAL** | **YES** | **INCOMPLETE** | `worker/service.py` | Messages route to DLQ topic, but no DLQ inspection/replay service or API exists. |

---

## 3. Deep Phase 9 Implementation Audit

### A. Workflow Querying & Discovery
- **Implemented**: `GET /api/workflows` supports `state`, `scenario`, `is_stuck`, `search`, `limit`, `offset`.
- **Flaw**: `FirestoreWorkflowStore` fetches all documents in the collection (`query.stream()`) and filters/slices in memory. At 50,000 workflows, this will cause memory exhaustion and multi-second latency.
- **Remedy**: Implement server-side Firestore `.limit()`, cursor-based pagination (`.start_after()`), and `.order_by("updated_at", direction=DESCENDING)`.

### B. Fleet Overview & Stuck Diagnostics
- **Implemented**: `GET /api/operator/overview` and `GET /api/operator/stuck-workflows` compute accurate stuck classifications and state counts.
- **Flaw**: Requires iterating over every workflow snapshot to evaluate stuck conditions.
- **Remedy**: Maintain an indexed `is_stuck` flag on the workflow document updated on state transitions and lease expirations, or query by `(state IN [CREATED, EXECUTING] AND updated_at < threshold)`.

### C. Workflow Cancellation
- **Implemented**: `POST /api/workflows/{id}/cancel` enforces role check (`OPERATOR`, `ADMIN`), transitions workflow to `ESCALATED`, logs immutable audit event, and rejects `COMPLETED` workflows.
- **Flaw**: If an asynchronous worker is actively executing a long-running step when cancellation occurs, the worker does not currently have an interruption signal / cancellation check token.
- **Remedy**: Worker step executor must check `workflow.state != ESCALATED` before each tool execution.

### D. Persistent Security Audit
- **Implemented**: `SecurityAuditEvent` model, asynchronous store hook, persistent `audit_events` storage, and `GET /api/audit/logs` query API with role gating.
- **Quality**: Excellent. Durability and immutability verified.

### E. Operator Console Web UI
- **Implemented**: Full single-page dashboard at `/console` with Fleet Overview, Workflow Explorer, Detail Drawer, Modals, and Action Hub.
- **Production Blockers**:
  1. `app.js` generates client-side mock JWTs with dummy signature (`mock_dev_signature`). In production (`config.is_production=True`), all API calls fail with HTTP 401.
  2. Native browser `EventSource` cannot send `Authorization` HTTP headers.
  3. Persona switcher dropdown is a development convenience that must be replaced with genuine authentication (e.g. login endpoint returning secure HTTP-only cookies or bearer tokens).

---

## 4. True System Maturity Level

**Current Classification**: **Level 2.5 — Advanced Functional Local Engine with Hardened Resilience Contracts, but Incomplete Distributed Execution and Local-Only Operator UI**.

- **Code Correctness**: 9/10 (State machine, OCC, leasing, and audit trail are mathematically sound).
- **Reliability & Resilience**: 9/10 (20 resilience scenarios verified under chaos injection).
- **Security & RBAC**: 8/10 (Double-token validation and tenant isolation solid at API layer; UI auth is simulated).
- **Distributed Execution**: 4/10 (Worker only transitions state; does not run agent tools).
- **Scalability**: 4/10 (Firestore queries do full collection scans; SSE polls database).
- **Operator UX Maturity**: 6/10 (Rich UI design, but currently tied to development mock credentials).

---

## 5. Evaluation of Candidate Phase 10 Directions

| Candidate Phase | Focus Area | Engineering ROI | Operator Value | Architectural Impact | Recommendation Rank |
|:---|:---|:---:|:---:|:---:|:---:|
| **Candidate 1: Full-Lifecycle Distributed Worker Execution** | Wire `AgentFactory`, tool executions, and outcome verification into the asynchronous Cloud Run Worker. | **CRITICAL (10/10)** | High | Closes the core product gap between async dispatch and actual workflow resolution. | **#1 (Primary Focus)** |
| **Candidate 2: Operator Console Production Hardening & Auth** | Real token/cookie auth session, SSE auth query token, remove dev persona bypass. | **HIGH (9/10)** | High | Makes the control plane usable in live staging and production. | **#2 (Integrated into Phase 10)** |
| **Candidate 3: Scalable Persistence & Indexing** | Server-side Firestore cursor pagination, `firestore.indexes.json`, event bus for SSE. | **HIGH (9/10)** | Medium | Prevents production database cost and memory explosion. | **#3 (Integrated into Phase 10)** |
| **Candidate 4: DLQ Triage & Replay Subsystem** | Dead-letter queue inspection, sanitization, and manual redrive UI. | Medium (6/10) | Medium | Valuable for operations, but secondary to making the primary worker execute. | **#4 (Deferred to Phase 11)** |

---

## 6. Recommended Phase 10: Full-Lifecycle Asynchronous Worker Execution & Production Platform

### 6.1 Phase 10 Core Objectives
1. **Complete Distributed Worker Agent Execution**:
   - Wire `WorkflowEventConsumer` to instantiate and execute the agent loop (`Taskmaster` + `AgentFactory` + `OnboardingTools` + `PolicyEngine`).
   - Guarantee that an asynchronous Pub/Sub message runs all required steps, verifies outcome contracts, and transitions the workflow to `COMPLETED` or `ESCALATED`.
   - Incorporate step-level cancellation checks so operator cancellations immediately halt in-flight worker execution.
2. **Productionize Operator Console Authentication & SSE**:
   - Implement `/api/auth/login` and `/api/auth/session` endpoints returning signed JWT tokens.
   - Update `/console` UI to support real login and secure token storage in `sessionStorage`.
   - Update `/api/workflows/{id}/events/stream` to accept authentication via query parameter (`?token=...`) or header, enabling browser `EventSource` support.
3. **Database Performance & Cursor-Based Pagination**:
   - Update `FirestoreWorkflowStore.list_workflows` to use native Firestore `.limit()`, `.start_after()`, and `.order_by()`.
   - Create `firestore.indexes.json` defining all required composite indexes (`tenant_id`, `state`, `updated_at`, `scenario`).
   - Implement lightweight in-memory event broadcast (`asyncio.Queue`) for active SSE connections to eliminate 1-second Firestore polling loops.
4. **Comprehensive Automated Verification**:
   - Dedicated Phase 10 test suite (`tests/test_phase10_worker_execution_and_platform.py`).
   - Verify asynchronous worker execution from `WORKFLOW_DISPATCH` to `COMPLETED` via Pub/Sub emulator and in-memory pipelines.
   - Verify production authentication flow, token validation, and SSE event streaming.

---

## 7. Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                               OPERATOR CONSOLE WEB UI (/console)                                │
│   ┌──────────────────────────┬───────────────────────────┬──────────────────────────────────┐   │
│   │ Real Token/Session Auth  │ Cursor-Based Explorer     │ Real-time EventSource Streaming  │   │
│   │ (No mock credentials)    │ (Server-side Pagination)  │ (Token Query Param / SSE Bus)    │   │
│   └──────────────────────────┴───────────────────────────┴──────────────────────────────────┘   │
└────────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                 │ HTTPS (Signed JWT Bearer)
                                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                     RECOVERYOS API SERVICE                                      │
│  - Real Authentication & Session Endpoint (`/api/auth/login`, `/api/auth/session`)              │
│  - Authenticated SSE Stream with In-Memory Event Broadcast (`/api/workflows/{id}/events/stream`)│
│  - Native Cursor-Based Firestore Queries (`limit`, `start_after`, `order_by`)                   │
│  - Operator Action Hub (`/api/workflows/{id}/recover`, `/api/workflows/{id}/cancel`)            │
└────────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                 │ Pub/Sub Publish (`recoveryos-workflow-execution`)
                                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 PRIVATE CLOUD RUN WORKER SERVICE                                │
│   ┌─────────────────────────────────────────────────────────────────────────────────────────┐   │
│   │ WorkflowWorkerService Ingress (Pub/Sub Push Envelope + OIDC Validation)                 │   │
│   │                                                                                         │   │
│   │ WorkflowEventConsumer                                                                   │   │
│   │   ├── 1. Tenant Isolation & Workflow Existence Gate                                     │   │
│   │   ├── 2. Terminal State Immutability Check (Drop if COMPLETED/ESCALATED)                │   │
│   │   ├── 3. Distributed Operation Claim (60s lease)                                        │   │
│   │   ├── 4. OCC Version Verification ($V == V_{expected}$)                                │   │
│   │   ├── 5. FULL AGENT EXECUTION LOOP (NEW)                                                │   │
│   │   │      ├── Instantiate AgentFactory, Taskmaster & OnboardingTools                     │   │
│   │   │      ├── Execute Workflow Steps with Policy Engine Enforcement                      │   │
│   │   │      ├── Check for Operator Cancellation before each step                           │   │
│   │   │      ├── Verify Outcome Contract Claims & Append Evidence                           │   │
│   │   │      └── Transition to COMPLETED (or ESCALATED on unresolved failure)               │   │
│   │   └── 6. Complete Operation Claim & ACK Message                                         │   │
│   └─────────────────────────────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                 │ Atomic Transactions & Snapshots
                                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                  CLOUD FIRESTORE (`recoveryosdb`)                               │
│  - Collections: `workflows`, `steps`, `events`, `evidence`, `operation_claims`, `audit_events`  │
│  - Schema Indexes: `firestore.indexes.json` (Composite indexes on `tenant_id` + `state` + time) │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 8. Components Affected & Exact File Changes

### Modified Files:
- [`backend/events/consumer.py`](file:///Users/urjasoft/Documents/Recovery%20OS/backend/events/consumer.py): Wire `AgentFactory`, `Taskmaster`, and full agent execution loop into `WorkflowEventConsumer`. Add step cancellation checks.
- [`backend/worker/server.py`](file:///Users/urjasoft/Documents/Recovery%20OS/backend/worker/server.py): Initialize `AgentFactory`, `SimulatedServices`, `FailureInjector`, and `PolicyEngine` for `WorkflowWorkerService`.
- [`backend/api/server.py`](file:///Users/urjasoft/Documents/Recovery%20OS/backend/api/server.py): Add `/api/auth/login` endpoint; update `/api/workflows/{id}/events/stream` to accept query token auth (`token: Optional[str] = None`); implement in-memory event broadcast subscriber.
- [`backend/persistence/workflow_store.py`](file:///Users/urjasoft/Documents/Recovery%20OS/backend/persistence/workflow_store.py): Optimize `FirestoreWorkflowStore.list_workflows` with server-side `.limit()`, `.order_by()`, and `.start_after()`.
- [`backend/api/static/app.js`](file:///Users/urjasoft/Documents/Recovery%20OS/backend/api/static/app.js): Remove client-side mock JWT generator; implement real login modal and signed token management in `sessionStorage`; pass auth token in SSE connection URL.

### New Files:
- [`firestore.indexes.json`](file:///Users/urjasoft/Documents/Recovery%20OS/firestore.indexes.json): Firestore composite index definitions for production.
- [`tests/test_phase10_worker_execution_and_platform.py`](file:///Users/urjasoft/Documents/Recovery%20OS/tests/test_phase10_worker_execution_and_platform.py): Comprehensive test suite covering end-to-end async worker agent execution, operator auth, Firestore cursor pagination, and SSE event streaming.
- [`docs/PHASE_10_COMPLETION_REPORT.md`](file:///Users/urjasoft/Documents/Recovery%20OS/docs/PHASE_10_COMPLETION_REPORT.md): Final closeout documentation.

---

## 9. Proposed Phase 10 Acceptance Gates

| Gate # | Milestone / Deliverable | Success Criteria |
|:---|:---|:---|
| **Gate 1** | Worker Agent Execution Loop Integration | Asynchronous worker executes `Taskmaster` steps, verifies contract outcomes, and transitions workflow to `COMPLETED`. |
| **Gate 2** | In-Flight Cancellation & Interruption Check | Worker checks `workflow.state` before each step and halts execution if state is `ESCALATED`. |
| **Gate 3** | Production Operator Authentication & Sessions | `/api/auth/login` issues high-entropy signed JWTs; `/console` UI uses real credentials without mock tokens. |
| **Gate 4** | Authenticated SSE Streaming & Event Broadcast | `/api/workflows/{id}/events/stream` supports query token authentication and event bus distribution. |
| **Gate 5** | Scalable Firestore Queries & Index Schema | `firestore.indexes.json` defined; `FirestoreWorkflowStore` uses server-side cursor pagination and limits. |
| **Gate 6** | Automated Verification & Regression Suite | 100% passing Phase 10 test suite; 100% full regression pass across all 325+ tests. |
| **Gate 7** | Final Documentation & Release Closeout | `docs/PHASE_10_COMPLETION_REPORT.md` published and verified. |

---

## 10. Production Safety & Invariants

- **Production Cloud Run**: UNTOUCHED (`recoveryos-00008-2bt` @ 100%).
- **Production Rollback Reserve**: UNTOUCHED (`recoveryos-00006-jwt` ready).
- **Zero Production Deployment**: All work performed locally in workspace.
- **Fail-Closed Production Configuration**: `validate_production_config()` remains strictly enforced.
