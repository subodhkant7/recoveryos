# RecoveryOS Phase 9 Proposal: Operator Control Plane & Recovery Console

**Target System**: RecoveryOS (`recoveryos-506713`)  
**Status**: DRAFT PROPOSAL FOR ARCHITECTURAL REVIEW  
**Author**: RecoveryOS Release & Systems Architecture Team  
**Phase Objective**: Transition RecoveryOS from a reliable backend execution engine into a unified, observable, operator-facing recovery platform.

---

## 1. Current-State Assessment

### Proven Production Baseline (Phases 1–8)
1. **Core Reliability & Asynchronous Orchestration**:
   - Multi-tier event-driven execution: API (`recoveryos-00008-2bt` @ 100% traffic) $\rightarrow$ Pub/Sub (`recoveryos-workflow-execution`) $\rightarrow$ Private Cloud Run Worker (`recoveryos-worker-00008-5pv`) $\rightarrow$ Cloud Firestore (`recoveryosdb`).
   - Zero-traffic rollback reserve revision verified and ready (`recoveryos-00006-jwt`).
   - Distributed Operation Claim leasing, monotonic OCC versioning ($V \rightarrow V+1$), and deterministic idempotency keys (`op_*`).
   - Fail-closed error classification (Retryable $\rightarrow$ NACK / 500, Permanent $\rightarrow$ DEAD_LETTER / 422).
   - Graceful shutdown task draining, distributed Gemini API token-bucket rate-limiting, and 20 verified failure-resilience scenarios across 307 automated tests.

2. **Existing Operator Capabilities**:
   - Authenticated diagnostic computation endpoint: `GET /api/workflows/{id}/diagnostics`.
   - Authorized, OCC-fenced recovery/redrive endpoint: `POST /api/workflows/{id}/recover`.
   - Role-Based Access Control (`Principal`, `Role.VIEWER`, `Role.OPERATOR`, `Role.APPROVER`, `Role.ADMIN`, `tenant_id`).
   - Prometheus metrics registry (`/metrics`) and structured JSON logs with correlation IDs.

---

## 2. Remaining Architectural & Product Gaps

Despite exceptional backend resilience, RecoveryOS currently lacks an operator-facing control plane:

1. **Workflow Discovery & Query Scalability Gap**:
   - `GET /api/workflows` returns an unpaginated, unfiltered dump of all workflows in memory/Firestore.
   - Operators cannot filter workflows by lifecycle state (`CREATED`, `EXECUTING`, `AWAITING_APPROVAL`, `RECOVERING`, `ESCALATED`, `COMPLETED`), scenario, date range, or stuck condition.
   - In a production environment with thousands of workflows, fetching all documents without pagination causes high latency, memory pressure, and potential timeout.

2. **Fleet-Wide Stuck Workflow Visibility Gap**:
   - Diagnostic analysis is currently strictly per-workflow (`/api/workflows/{id}/diagnostics`).
   - There is no aggregate endpoint (e.g., `/api/operator/stuck-workflows` or `/api/operator/fleet/overview`) to identify all stalled workflows across a tenant in one query. Operators must know the workflow ID in advance.

3. **Audit Trail Persistence & Queryability Gap**:
   - Security and operator audit logs (`record_security_audit_event`) currently reside only in a transient Python list in process memory (`_SECURITY_AUDIT_LOGS`) and stdout.
   - Audit records are lost on server restart and cannot be retrieved or inspected via an operator API (`/api/audit/logs`).

4. **Operator Action Taxonomy & Guardrails**:
   - Lack of distinct, structured operator action primitives:
     - **Inspect / Diagnose**: Non-mutating health & lease evaluation.
     - **Redrive / Recover**: Resuming stuck/interrupted executions with OCC version binding.
     - **Approve / Reject**: Deciding pending human-in-the-loop gates.
     - **Cancel / Escalate**: Gracefully terminating runaway workflows with recorded justification.
   - Lack of two-person or explicit confirmation guardrails for high-impact operator interventions.

5. **Absence of an Operator Console UI**:
   - There is no graphical user interface. All triage, inspection, recovery, and approval actions require crafting manual `curl` requests with raw JWT Bearer tokens or inspecting raw GCP Cloud Logging logs.
   - Operators cannot visualize workflow progression, event streams, evidence artifacts, or system health metrics in real time.

---

## 3. Why Phase 9 is the Highest-Value Next Step

RecoveryOS has completed extensive reliability engineering (Phases 6.4, 6.5, 7, and 8). The fundamental barrier to operational adoption is no longer infrastructure reliability—it is **operational ergonomics, actionable visibility, and fast incident resolution**.

An **Operator Control Plane & Recovery Console**:
1. **Reduces MTTR (Mean Time to Resolution)**: Operators can identify, inspect, and safely recover stalled workflows in seconds rather than diagnosing via raw command-line tools and database queries.
2. **Eliminates Manual Human Errors**: Provides guardrailed, OCC-protected action buttons with explicit validation rather than error-prone manual API calls.
3. **Provides Unified Visibility**: Combines real-time event streaming (SSE), diagnostic metrics, evidence inspection, and audit logs into a single coherent interface.
4. **Maintains Enterprise Tenant Security**: Enforces tenant boundaries, role-based action gating, and tamper-proof audit trails for all operator actions.

---

## 4. Explicit Non-Goals

To maintain focus and avoid scope creep, the following are explicitly **out of scope** for Phase 9:
- **NO redesign of the underlying workflow execution engine or state machine**.
- **NO replacement of Google Cloud Pub/Sub or Firestore persistence layer**.
- **NO heavy external UI frameworks or complex multi-container frontend deployments**. The operator console will be built with clean, modern, zero-dependency HTML5/Vanilla CSS/ES Modules served directly by FastAPI or a lightweight static asset mount.
- **NO arbitrary or non-idempotent workflow data mutation**. Operators cannot edit raw workflow state in Firestore; all state mutations must flow through deterministic engine transitions and OCC-guarded events.
- **NO unauthenticated or public exposure of the private worker**. The worker remains strictly private and protected by Google Cloud IAM OIDC.

---

## 5. Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                              OPERATOR CONTROL PLANE (UI)                                │
│   ┌───────────────────────┬──────────────────────────┬──────────────────────────────┐   │
│   │    Fleet Dashboard    │    Workflow Explorer     │   Workflow Detail & Action   │   │
│   │ (Health, Backlog, DLQ)│ (Search, Filter, Paging) │ (Timeline, SSE, Redrive, HITL)│  │
│   └───────────────────────┴──────────────────────────┴──────────────────────────────┘   │
└─────────────────────────────────────────┬───────────────────────────────────────────────┘
                                          │ HTTPS + JWT Bearer Auth (RBAC / Tenant Isolated)
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                 RECOVERYOS API SERVICE                                  │
│   ┌─────────────────────────────────────────────────────────────────────────────────┐   │
│   │                        FastAPI Application Core                                 │   │
│   │  - Static Asset Server (`/console`)                                             │   │
│   │  - Workflow Query & Filter Engine (`/api/workflows?state=&is_stuck=...`)        │   │
│   │  - Fleet & Health Diagnostics (`/api/operator/overview`, `/api/operator/stuck`) │   │
│   │  - Action Hub (`/api/workflows/{id}/recover`, `/api/workflows/{id}/cancel`)     │   │
│   │  - Persistent Security Audit Engine (`/api/audit/logs`)                         │   │
│   │  - Real-time Event Streaming (`/api/workflows/{id}/events/stream` via SSE)      │   │
│   └────────────────────────────────────────┬────────────────────────────────────────┘   │
└────────────────────────────────────────────┼────────────────────────────────────────────┘
                                             │
                      ┌──────────────────────┴──────────────────────┐
                      ▼                                             ▼
         ┌─────────────────────────┐                   ┌─────────────────────────┐
         │     Cloud Firestore     │                   │     Google Cloud Pub/Sub│
         │     (`recoveryosdb`)    │                   │ (`recoveryos-workflow-  │
         │ - Workflows, Steps      │                   │      execution`)        │
         │ - Audit Logs (New)      │                   └────────────┬────────────┘
         │ - Operation Claims      │                                │ OIDC Push
         └─────────────────────────┘                                ▼
                                                       ┌─────────────────────────┐
                                                       │  Private Cloud Run      │
                                                       │  Worker Service         │
                                                       └─────────────────────────┘
```

---

## 6. API Changes Required

### 6.1 Workflow Discovery & Querying
- **`GET /api/workflows` (Enhanced)**:
  - Query parameters:
    - `state: Optional[WorkflowState]`
    - `scenario: Optional[str]`
    - `is_stuck: Optional[bool]`
    - `limit: int = 50` (max 100)
    - `offset: int = 0`
    - `search: Optional[str]` (matches workflow_id or customer name)
  - Returns paginated list + total count metadata.

### 6.2 Fleet & Operational Overview
- **`GET /api/operator/overview`**:
  - Gated to `VIEWER`, `OPERATOR`, `APPROVER`, `ADMIN`.
  - Returns aggregated statistics scoped to caller's tenant:
    - Counts by state (`CREATED`, `EXECUTING`, `AWAITING_APPROVAL`, `RECOVERING`, `ESCALATED`, `COMPLETED`).
    - Total stuck workflow count.
    - Pending human approval count.
    - System health summary & recent error count.
- **`GET /api/operator/stuck-workflows`**:
  - Returns all workflows currently classified as `is_stuck == True` across the tenant.

### 6.3 Operator Action Hub
- **`POST /api/workflows/{workflow_id}/recover` (Preserved & Enhanced)**:
  - Role-gated to `OPERATOR`, `ADMIN`.
  - Requires justification reason.
  - OCC version checking & fresh idempotency key dispatch.
- **`POST /api/workflows/{workflow_id}/cancel` (New)**:
  - Role-gated to `OPERATOR`, `ADMIN`.
  - Safely transitions active or stuck workflow to `ESCALATED` with reason and audit event.

### 6.4 Persistent Audit Logs
- **`GET /api/audit/logs` (New)**:
  - Role-gated to `ADMIN`, `OPERATOR`.
  - Query parameters: `workflow_id`, `actor_id`, `event_type`, `limit`, `offset`.
  - Returns persistent audit trail records.

---

## 7. Data-Model Changes Required

### 7.1 Security Audit Record Model (`backend/models/audit.py`)
```python
class SecurityAuditEvent(BaseModel):
    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: str  # PRIVILEGED_MUTATION, RECOVERY_TRIGGERED, APPROVAL_DECIDED, CANCEL_TRIGGERED, AUTH_DENIAL
    actor_id: str
    role: str
    tenant_id: str
    workflow_id: Optional[str] = None
    action: str
    outcome: str  # SUCCESS, DENIED, FAILED
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 7.2 Persistence Layer Extension (`backend/persistence/workflow_store.py`)
- Add `save_audit_event(event_data: dict[str, Any]) -> None`
- Add `list_audit_events(tenant_id: Optional[str], workflow_id: Optional[str], limit: int, offset: int) -> list[dict[str, Any]]`
- Add filtering and pagination support to `list_workflows(tenant_id, state, limit, offset)`.

---

## 8. Security & RBAC Model

| Role | Workflow Read | Diagnostics | Approve / Reject | Redrive / Recover | Cancel Workflow | View Audit Logs |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **VIEWER** | Yes | Yes | No | No | No | No |
| **APPROVER** | Yes | Yes | **Yes** | No | No | No |
| **OPERATOR** | Yes | Yes | No | **Yes** | **Yes** | **Yes** |
| **ADMIN** | Yes | Yes | **Yes** | **Yes** | **Yes** | **Yes** |

**Guarantees**:
- Strict tenant boundary isolation (non-admin callers can never query, inspect, or mutate other tenants' workflows or audit logs).
- Dual-layer token verification with HMAC-SHA256 signature verification.
- Complete secret redaction in all log emissions and API responses.

---

## 9. Audit Model

Every state-changing operator interaction automatically generates an immutable audit record:
1. **Who**: Principal `user_id` + `role`.
2. **What**: Specific action (`RECOVER_WORKFLOW`, `APPROVE_PLAN`, `REJECT_PLAN`, `CANCEL_WORKFLOW`).
3. **Where**: Target `workflow_id` + `tenant_id`.
4. **When**: UTC ISO timestamp.
5. **Why**: Mandatory operator justification text.
6. **Result**: `SUCCESS` / `DENIED` / `FAILED` with correlation ID and OCC version.

---

## 10. UI/UX Scope: Operator Recovery Console

A modern, high-contrast, responsive Operator Console served at `/console`:

1. **Top Navigation & Tenant / Role Context Bar**:
   - Current caller badge (`Operator: alice@acme.com`, `Role: OPERATOR`, `Tenant: tenant-acme`).
   - Token switcher / mock login selector for quick operator persona switching in development/testing.
   - Global auto-refresh toggle (5s, 15s, 30s, Off).

2. **Fleet Overview Dashboard Tab**:
   - Metric summary cards: Active Workflows, Stuck Workflows (Amber Alert), Approvals Waiting (Blue Alert), Escalated/Failed (Red Alert), Completed.
   - Quick Filter shortcuts ("View All Stuck", "View Pending Approvals").

3. **Workflow Explorer Tab**:
   - Interactive search & filter controls (State filter dropdown, scenario dropdown, search bar).
   - Rich data table: ID, Scenario, State badge, OCC Version, Age, Lease Owner, Stuck indicator, Action buttons.
   - Pagination controls.

4. **Workflow Detail & Timeline Inspector Drawer / View**:
   - Live state banner and outcome contract checklist.
   - Step execution table with output artifacts & evidence verification hashes.
   - Real-time Server-Sent Events (SSE) stream terminal showing live lifecycle events.

5. **Operator Action Hub**:
   - **Diagnostics Modal**: Visual breakdown of why a workflow is stuck, lease expiration timer, and recovery recommendation.
   - **Safe Redrive / Recover Modal**: Displays current OCC version $V$, prompts for required reason, and dispatches recovery message.
   - **HITL Approval Panel**: Review proposed recovery plan steps, impact assessment, and Approve/Reject controls.

6. **Security & Audit Log Viewer Tab**:
   - Filterable timeline of all privileged mutations, recovery actions, and security access decisions.

---

## 11. Test Strategy

1. **Backend Unit & Integration Tests**:
   - `tests/test_phase9_operator_api.py`:
     - Test paginated & filtered `GET /api/workflows`.
     - Test fleet overview calculation `GET /api/operator/overview`.
     - Test stuck workflow aggregator `GET /api/operator/stuck-workflows`.
     - Test cancel workflow endpoint `POST /api/workflows/{id}/cancel`.
     - Test persistent audit log storage and query endpoints `GET /api/audit/logs`.
     - Test RBAC enforcement across VIEWER, APPROVER, OPERATOR, and ADMIN roles.
     - Test multi-tenant isolation under all query and action filters.
2. **Full Regression Test Suite**:
   - Maintain 100% pass rate across all existing 307 tests (Phases 1–8).
3. **UI Contract & Static Serving Tests**:
   - Test console static asset routing, bundle integrity, and API endpoint compatibility.

---

## 12. Migration & Backward-Compatibility Considerations

- All new query parameters on `GET /api/workflows` will have default values (`limit=50`, `offset=0`), preserving backward-compatibility for existing callers.
- Existing routes (`/api/workflows/{id}`, `/api/workflows/{id}/diagnostics`, `/api/workflows/{id}/recover`, `/api/health`, `/metrics`) remain 100% backward-compatible.
- Firestore document schema for workflows remains unchanged; audit logs use a separate `audit_events` collection.

---

## 13. Production Rollout Strategy

1. **Zero-Downtime Deployment**:
   - Deploy as a zero-traffic revision to Cloud Run (`recoveryos`).
   - Run live non-destructive verification against the staging/candidate revision.
   - Execute controlled 10% canary traffic shift, observe error rates and metrics, then promote to 100%.

---

## 14. Rollback Strategy

- In the event of any regression, immediate instant traffic shift back to `recoveryos-00008-2bt` (100%) via `gcloud run services update-traffic recoveryos --to-revisions=recoveryos-00008-2bt=100`.

---

## 15. Acceptance Gates

| Gate # | Milestone / Deliverable | Success Criteria |
|:---|:---|:---|
| **Gate 1** | Filtered & Paginated Workflow Query Engine | `GET /api/workflows` supports state, scenario, stuck filters & pagination with tenant isolation. |
| **Gate 2** | Fleet Diagnostics & Overview Endpoints | `GET /api/operator/overview` and `/api/operator/stuck-workflows` return accurate aggregate health metrics. |
| **Gate 3** | Operator Action Hub & Workflow Cancellation | `POST /api/workflows/{id}/cancel` safely transitions to ESCALATED; recovery endpoint verified. |
| **Gate 4** | Persistent Audit Logging Subsystem | Audit events persisted in Firestore / in-memory store; queryable via `GET /api/audit/logs`. |
| **Gate 5** | Operator Console UI Implementation | Interactive HTML5/CSS/JS web interface served at `/console` with real-time SSE updates. |
| **Gate 6** | Automated Test Suite & RBAC Verification | Full Phase 9 automated test suite passing; 100% regression suite passing. |
| **Gate 7** | Phase 9 Completion & Release Documentation | Comprehensive documentation in `docs/PHASE_9_OPERATOR_CONTROL_PLANE.md`. |

---

## 16. Risks and Failure Modes

| Risk | Mitigation Strategy |
|:---|:---|
| High query latency on unindexed Firestore fields | Index `tenant_id`, `state`, `created_at` in Firestore; enforce server-side pagination limits ($N \le 100$). |
| Cross-tenant data leakage via operator console | Enforce strict `principal.can_access_tenant()` checks in all query handlers; sanitize responses. |
| Accidental operator redrive of healthy executing workflows | Diagnostic pre-check verifies `is_stuck` or `ESCALATED`; UI requires explicit confirmation. |
| UI credential exposure | JWT tokens kept in browser sessionStorage / memory only; never logged or serialized to disk. |
