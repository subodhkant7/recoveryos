# Phase 5.6: RecoveryOS Production Threat Model & Attack Surface Analysis

---

## 1. Threat Matrix

| Threat ID | Threat / Attack Vector | Attack / Failure Scenario | Current Protection Mechanism | Empirical Evidence | Residual Risk & Edge Cases | Severity |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **THREAT-01** | Forged JWT Token | Attacker crafts a token with modified `role: admin` and forged signature | Cryptographic HMAC-SHA256 signature verification using server secret | `AUTH-04` PASS (HTTP 401) | Secret key compromise exposes system if key is weakly generated | **HIGH** (Controlled by fail-closed config) |
| **THREAT-02** | Expired JWT Replay | Attacker replays intercepted token after expiration | `verify_access_token` checks `exp < now` | `AUTH-03` PASS (HTTP 401) | Clock skew $> 60\text{s}$ between server nodes | **MEDIUM** |
| **THREAT-03** | Privilege Escalation | User with `VIEWER` role calls `launch_scenario` or `approve_workflow` | FastAPI RBAC dependencies (`require_role(OPERATOR, ADMIN)`) | `AUTH-07`, `AUTH-08` PASS (HTTP 403) | Misconfigured route missing `Depends(require_role)` | **HIGH** (Gated by code review/route audit) |
| **THREAT-04** | Cross-Tenant Data Access | Tenant A requests or modifies Tenant B's workflow | `_get_authorized_workflow` enforces `principal.can_access_tenant(wf_tenant)` | `AUTH-16` PASS (HTTP 403) | Unscoped background jobs referencing global collections without tenant filters | **CRITICAL** (Requires tenant filter in queries) |
| **THREAT-05** | Approval Forgery / Replay | Attacker sends approval with fake `decided_by` or duplicate submission | Server ignores request body `decided_by` and stamps authenticated JWT principal; status checked for `PENDING` | `AUTH-11`, `AUTH-14` PASS | Approver token theft allows valid approvals within lease window | **HIGH** |
| **THREAT-06** | Duplicate External Mutation | Concurrent workers execute same action (e.g. `setup_billing`) simultaneously | Canonical idempotency keys + `claim_operation` lease acquiring | `CONC-01`, `CONC-02` PASS | Database partition during lease check when fallback store is non-distributed | **HIGH** |
| **THREAT-07** | Worker Crash Mid-Mutation | Worker dies after external API succeeds but before recording result | Reconciler re-queries external provider state before attempting mutation | `CONC-04`, `DUR-06` PASS | External provider lacks queryable state/idempotency API | **MEDIUM** |
| **THREAT-08** | Firestore Outage / Network Drop | Cloud Firestore unreachable during write | Readiness probe (`/api/ready`) fails 503; OCC transaction aborts cleanly | `HEALTH-03` PASS | Read-only degraded mode not currently supported for queries | **MEDIUM** |
| **THREAT-09** | Stale OCC Write Race | Two workers attempt concurrent updates to same workflow | OCC version check `current_ver == expected_version` throws `StaleWorkflowStateError` | `DUR-07`, `DUR-08` PASS | Client retry storm if backoff jitter is not used | **LOW** |
| **THREAT-10** | Gemini Rate Limiting (429) | LLM quota exhausted during peak traffic | `ResilientGemini` enforces $\ge 6.5\text{s}$ pacing, bounded backoff, and Retry-After delay | `GEM-01`, `GEM-03`, `GEM-05` PASS | Rate limiter is in-process; multi-container clusters can aggregate above RPM | **HIGH** (Requires global Redis/GCP limiter in multi-replica) |
| **THREAT-11** | Gemini Full Outage / 503 | Gemini API completely down | Circuit breaker opens after 5 failures; fails fast with `CircuitOpenError` | `GEM-15`, `GEM-16` PASS | Workflows remain in `UNKNOWN`/`RECOVERING` pending manual operator intervention | **MEDIUM** |
| **THREAT-12** | Unhandled Agent Exception | Agent crashes/times out during execution | `_run_agent` transitions workflow state to `UNKNOWN` and writes audit event | `GEM-11`, `GEM-12` PASS | None (eliminates permanently stranded `EXECUTING` workflows) | **LOW** |
| **THREAT-13** | Prompt Injection via Data | Attacker injects system prompt overrides into `customer_data` or tool errors | Deterministic `PolicyEngine` and `OutcomeContract` sovereign checks ignore LLM claims | `ADV-09`, `ADV-11` PASS | LLM may hallucinate plans, but deterministic engine rejects execution | **LOW** |
| **THREAT-14** | Terminal State Mutation | Attacker attempts to resume or approve a `COMPLETED` or `ESCALATED` workflow | Engine `VALID_TRANSITIONS` rejects all transitions out of terminal states | `AUTH-15`, `DUR-09` PASS | None (Terminal states are strictly immutable) | **LOW** |
| **THREAT-15** | Log Secret Leakage | API keys or tokens embedded in customer input or error messages | `StructuredJsonFormatter` recursively redacts matching keys and JWT/API key regex | `OBS-02`, `AUTH-17` PASS | Unstructured binary dumps or base64-encoded secret payloads | **LOW** |
| **THREAT-16** | Metric Cardinality Explosion | High-volume traffic with unique UUIDs causes Prometheus memory leak | `_sanitize_path_for_metric` maps UUIDs and IDs to `:id` and `:approval_id` | `OBS-04` PASS | Query parameters in custom paths if not routed through sanitizer | **LOW** |
| **THREAT-17** | Ungraceful Container SIGTERM | Process killed immediately, leaving in-flight mutations unrecorded | `ShutdownManager` traps SIGTERM, rejects new tasks, and drains active tasks (5s) | `SHUTDOWN-01`..`03` PASS | External API taking $> 5\text{s}$ to return when SIGKILL is sent | **MEDIUM** |

---

## 2. Critical Production Hardening Insights
1. **Multi-Container Rate Limiting:** The `GeminiRateLimiter` is in-process async token queue. If 10 container replicas are deployed, aggregate requests could hit 100 RPM against a 15 RPM quota. In multi-replica cloud deployment (Phase 6), a distributed Redis rate limiter or GCP Cloud Tasks queue is required.
2. **Tenant Filter Invariants:** While single workflow lookups enforce tenant isolation (`_get_authorized_workflow`), collection-level listing endpoints must always pass `tenant_id` filters to prevent cross-tenant list leaks.
