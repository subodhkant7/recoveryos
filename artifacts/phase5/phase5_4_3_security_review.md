# Phase 5.4.3: Production API Security, Authentication & RBAC Review

---

## 1. Executive Summary
Phase 5.4.3 secures the RecoveryOS API surface by introducing cryptographically signed HMAC-SHA256 JWT authentication, deterministic Role-Based Access Control (RBAC), multi-tenant isolation, structured security audit logging, and authoritative identity stamping on human approval gates.

---

## 2. Authentication Design & Token Subsystem
- **Mechanism:** Signed HMAC-SHA256 JWT tokens with configurable secret key (`JWT_SECRET_KEY`), algorithm (`JWT_ALGORITHM="HS256"`), and lifetime (`JWT_EXPIRATION_MINUTES=60`).
- **Signature Verification:** Constant-time `hmac.compare_digest` verification.
- **Header & Algorithm Validation:** Explicitly enforces `alg == "HS256"`. Tokens specifying `"none"`, asymmetric keys, or unsupported algorithms are rejected with `HTTP 401`.
- **Claim Verification:** Strict evaluation of `exp` (expiration), `iat` (issued-at), `sub` (user_id), `role` (valid enum member), and `tenant_id`.
- **Credential Hygiene:** Tokens, raw headers, and secrets are strictly redacted and never output to application or audit logs.

---

## 3. Role-Based Access Control (RBAC) Matrix

### Defined Roles & Granular Permissions
- **`viewer`**: Granted `workflow:read`. Can inspect workflows, timelines, evidence, and approvals within their assigned tenant.
- **`operator`**: Granted `workflow:read`, `workflow:operate`. Can launch workflows and initiate scenarios within their assigned tenant.
- **`approver`**: Granted `workflow:read`, `workflow:approve`. Can approve or reject human approval gates.
- **`admin`**: Granted `workflow:read`, `workflow:operate`, `workflow:approve`, `admin:all`. Unrestricted cross-tenant oversight.

### Endpoint Authorization Matrix

| Endpoint | HTTP Method | Security Classification | Required Role | Tenant Filter Enforced |
| :--- | :--- | :--- | :--- | :--- |
| `/api/health` | `GET` | **PUBLIC** | None | No |
| `/api/scenarios/{scenario_name}` | `POST` | **AUTHENTICATED** | `operator`, `admin` | Yes (tagged to principal tenant) |
| `/api/workflows` | `GET` | **AUTHENTICATED** | `viewer`, `operator`, `approver`, `admin` | Yes (filtered to principal tenant) |
| `/api/workflows/{id}` | `GET` | **AUTHENTICATED** | `viewer`, `operator`, `approver`, `admin` | Yes (`_get_authorized_workflow`) |
| `/api/workflows/{id}/events` | `GET` | **AUTHENTICATED** | `viewer`, `operator`, `approver`, `admin` | Yes (`_get_authorized_workflow`) |
| `/api/workflows/{id}/events/stream` | `GET` | **AUTHENTICATED** | `viewer`, `operator`, `approver`, `admin` | Yes (`_get_authorized_workflow`) |
| `/api/workflows/{id}/approvals` | `GET` | **AUTHENTICATED** | `viewer`, `operator`, `approver`, `admin` | Yes (`_get_authorized_workflow`) |
| `/api/workflows/{id}/approve/{approval_id}` | `POST` | **AUTHENTICATED** | `approver`, `admin` | Yes (`_get_authorized_workflow`) |

---

## 4. Human Approval Security Model
On `POST /api/workflows/{workflow_id}/approve/{approval_id}`:
1. **Authoritative Caller Stamping:** Any client-supplied `decided_by` or spoofed identity is ignored. The verified `principal.user_id` from the JWT token is stamped directly into `approval["decided_by"]`.
2. **Strict Workflow & Approval Binding:** The approval record must belong to the exact `workflow_id` specified in the path and the caller's tenant.
3. **Pending Status Check:** Only approvals with status `PENDING` can be transitioned. Duplicate approval or rejection attempts return `HTTP 400`.
4. **Terminal State Protection:** Approvals on workflows in `COMPLETED` or `ESCALATED` states are rejected with `HTTP 400`.
5. **Preserved Policy Engine Boundary:** Human approvals only unlock the specific tool and arguments validated by the policy engine; arbitrary parameter tampering is blocked.

---

## 5. Threat-Model Review Findings

| Threat Category | Risk Assessment | Mitigation Implemented |
| :--- | :--- | :--- |
| **Privilege Escalation** | High | Roles are derived strictly from cryptographically signed JWT claims. Body parameters (e.g. `{"role": "admin"}`) are ignored by the FastAPI dependency layer. |
| **Confused Deputy** | High | The LLM agent operates under an internal execution context (`actor="taskmaster"`). Mutating tools called by agents are gated by `PolicyEngine`; API endpoints are gated by `require_role`. |
| **IDOR / BOLA** | High | All resource queries validate `principal.can_access_tenant(wf_tenant)`. Unauthorized cross-tenant access yields `HTTP 403`. |
| **Forged Approval Identity** | High | Client request bodies cannot set `decided_by`. The decider identity is authoritatively populated from `principal.user_id`. |
| **Replayed Approval Requests** | Medium | State transitions on approvals are idempotent and monotonic (`PENDING` $\rightarrow$ `APPROVED`/`REJECTED`). Subsequent attempts fail with `HTTP 400`. |
| **Cross-Tenant Access** | High | Every workflow and approval is tagged with `tenant_id`. Workflows are isolated unless accessed by `Role.ADMIN`. |
| **JWT Algorithm/Key Confusion** | High | Token parser strictly asserts `alg == "HS256"`. `"none"` and asymmetric algorithms are rejected. |
| **Expired Token Acceptance** | Medium | Token verification validates `exp > current_timestamp` prior to parsing claims. |
| **Tool Argument Authorization Bypass** | High | Tool execution passes through deterministic `PolicyEngine.evaluate()`, ensuring constraints and step ordering cannot be bypassed by argument tampering. |

---

## 6. Verification Results

### Deterministic Test Battery
- **Core Deterministic Tests:** 41 / 41 PASS
- **Adversarial Evaluation Tests (Phase 5.2):** 21 / 21 PASS
- **Durable Persistence Tests (Phase 5.4.1):** 10 / 10 PASS
- **Distributed Concurrency Tests (Phase 5.4.2):** 12 / 12 PASS
- **API Security Suite (Phase 5.4.3):** 20 / 20 PASS
- **Firestore Emulator Integration:** 3 SKIPPED (Emulator inactive)
- **Total Deterministic Tests:** **104 / 104 PASSED (100% in 1.02s)**

### Live Gemini Evaluation
- **Scenario A (Dynamic Provider Selection):** LIVE GEMINI PASS (PayPal)
- **Scenario B (Constraint Filtering):** LIVE GEMINI PASS (Square)
- **Scenario C (Negative Refusal):** LIVE GEMINI PASS (0 plans generated)
- **Scenarios D/E/F (Policy, Resumption, Boundaries):** LIVE PASS
- **Total Live Scenarios:** **7 / 7 PASSED (100% in 27.28s)**

---

## 7. Remaining Production Blockers
1. **Phase 5.4.4 (Runtime Gemini Quota Protection):** Global rate limiter and 429 adaptive backoff directly within the live server runtime loop.
2. **Phase 5.4.5 (Observability & Containerization):** OpenTelemetry distributed tracing, Prometheus metrics export (`/metrics`), and production Dockerfile.
