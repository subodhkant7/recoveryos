# Phase 5.5: RecoveryOS Comprehensive Failure & Crash Matrix

---

| ID | Failure Point | Expected State | Actual State | External Side-Effect | Reconciliation Behavior | Duplicate-Mutation Risk | Evidence |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **A** | Crash before external mutation | `UNKNOWN` / `EXECUTING` | `UNKNOWN` | None | Worker re-acquires claim and performs mutation | **NONE** (No external call made) | `CONC-03` / `DUR-06` PASS |
| **B** | Crash after external mutation | `UNKNOWN` | `UNKNOWN` | 1 external call succeeded | Reconciler queries external provider, records completion without re-mutating | **NONE** (Idempotency key prevents duplicate call) | `CONC-04` / `DUR-06` PASS |
| **C** | Crash before persistence write | `UNKNOWN` | `UNKNOWN` | None | Restart reconciles workflow state from external truth | **NONE** (OCC & claims gate retries) | `DUR-06` PASS |
| **D** | Crash after persistence write | `UNKNOWN` | `UNKNOWN` | 1 external call succeeded | Next restart inspects completed claim, skips tool execution | **NONE** (Stored result returned) | `DUR-03` / `CONC-02` PASS |
| **E** | Gemini HTTP 429 Quota Exhausted | `RECOVERING` / Retrying | `RECOVERING` | None (Reasoning turn only) | Centralized backoff ($2\text{s} \rightarrow 30\text{s}$) with jitter and Retry-After delay | **NONE** (Mutations never in LLM retry loop) | `GEM-03` / `GEM-05` PASS |
| **F** | Gemini HTTP 503 Service Unavailable | `RECOVERING` / Retrying | `RECOVERING` | None (Reasoning turn only) | Bounded retries up to 3 attempts, falls back to `UNKNOWN` on exhaustion | **NONE** (No tools invoked) | `GEM-07` PASS |
| **G** | Gemini Request Timeout (>30s) | `UNKNOWN` / Retrying | `UNKNOWN` | None | `asyncio.wait_for` cancels request; transitions workflow to `UNKNOWN` | **NONE** (No unhandled stuck `EXECUTING` states) | `GEM-11` / `GEM-12` PASS |
| **H** | Provider HTTP 404 (Entity Not Found) | `RECOVERING` | `RECOVERING` | None | Failure recorded in step history; agent reasons alternative path | **NONE** | Core Test Battery PASS |
| **I** | Provider HTTP 200 with Error Payload | Step `FAILED` | Step `FAILED` | None | Tool parser inspects body and rejects false success | **NONE** | `ADV-19` PASS |
| **J** | Stale OCC Version Update Race | Conflict Rejected | `StaleWorkflowStateError` | None | Transaction aborts; second worker must re-read latest version | **NONE** (OCC aborts overwrite) | `DUR-07` / `DUR-08` PASS |
| **K** | Duplicate Concurrent Operation | Claim Denied | `IN_PROGRESS` or Cached Result | Exactly 1 mutation | Worker B receives existing claim and reuses outcome | **NONE** | `CONC-01` / `CONC-02` PASS |
| **L** | Terminal Workflow Mutation Attempt | `HTTP 400` / Invalid Transition | `COMPLETED` / `ESCALATED` | None | Engine and API reject any state transition or tool execution | **NONE** (Terminal state immutable) | `DUR-09` / `AUTH-15` PASS |
| **M** | Invalid / Unauthorized Human Approval | `HTTP 401` / `HTTP 403` / Rejected | Unchanged | None | Token role checked; unauthorized actors rejected | **NONE** | `AUTH-08` / `AUTH-11` PASS |
| **N** | Firestore Unavailable | `HTTP 503` (Readiness) | Not Ready | None | `/api/ready` probe fails; application refuses traffic | **NONE** (Fails closed) | `HEALTH-03` PASS |
| **O** | Process Restart / Crash | Incomplete workflows recovered | `WORKFLOW_RESUMED` | None duplicated | Startup scans incomplete workflows, reconciles, and resumes | **NONE** | `DUR-01` / `DUR-05` PASS |
| **P** | Container Restart / SIGTERM | Graceful drain (5s) | Clean exit | None | `ShutdownManager` stops accepting new tasks and drains pending tasks | **NONE** | `SHUTDOWN-01`–`03` PASS |
