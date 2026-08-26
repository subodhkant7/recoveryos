# Phase 5.5: Architectural Claims Audit

---

| Architectural Claim | Audit Status | Verification Evidence | Known Limitation / Qualification |
| :--- | :--- | :--- | :--- |
| **Durable In-Memory Persistence** | **PROVEN** | `tests/test_durable_persistence.py` (10/10 PASS) | Process-local; requires external storage across real distributed nodes. |
| **Firestore Production Transactions & OCC** | **UNVERIFIED** | Code implemented in `FirestoreWorkflowStore`; skipped in tests | Firestore emulator was not installed/running on the host system. |
| **Outcome Verification Sovereignty** | **PROVEN** | Core deterministic tests + `tests/test_verification_contract.py` | Verification relies on deterministic tools and evidence matching. |
| **Policy Engine Superiority** | **PROVEN** | Core tests + `tests/test_adversarial_evaluation.py` (21/21 PASS) | LLMs cannot bypass or forge policy decisions. |
| **Distributed Idempotency & Claims** | **PROVEN** | `tests/test_distributed_concurrency.py` (12/12 PASS) | Proven for in-memory and simulated store contracts. |
| **Real Multi-Process Concurrency** | **PROVEN** | `tests/test_multiprocess_concurrency.py` (1/1 PASS) | Separate OS processes spawn cleanly and isolate execution. |
| **JWT Authentication & RBAC** | **PROVEN** | `tests/test_api_security.py` (20/20 PASS) | Covers expired tokens, forged roles, wrong secrets, signature tampering. |
| **Tenant Isolation** | **PROVEN** | `AUTH-16` / `GEM-14` / `test_api_security.py` | Cross-tenant workflow reads and mutations return HTTP 403. |
| **Authoritative Human Approval Gate** | **PROVEN** | `tests/test_human_approval.py` + `AUTH-08..15` | Authenticated principal from JWT overrides forged body metadata. |
| **Terminal Workflow Immutability** | **PROVEN** | `DUR-09`, `CONC-07`, `AUTH-15` | `COMPLETED` and `ESCALATED` workflows reject all further mutations. |
| **Gemini Runtime Rate Limiting** | **PROVEN** | `tests/test_gemini_resilience.py` (GEM-01, GEM-02) + Live Eval | Centralized async queue guarantees $\ge 6.5\text{s}$ spacing ($\le 10\text{ RPM}$). |
| **Gemini Circuit Breaking & Jittered Backoff**| **PROVEN** | `GEM-03` through `GEM-19` | Exponential backoff bounded at 30s; circuit opens after 5 failures. |
| **Crash-Safe Workflow State (`UNKNOWN`)** | **PROVEN** | `GEM-11`, `GEM-12` | Unhandled agent crashes transition workflow to `UNKNOWN`. |
| **Structured JSON Logging & Redaction** | **PROVEN** | `tests/test_observability.py` | Recursive PII and credential masking verified. |
| **Request Correlation (`X-Request-ID`)** | **PROVEN** | `tests/test_observability.py` | Contextvars propagation and response header reflection verified. |
| **Prometheus Metrics (`/metrics`)** | **PROVEN** | `tests/test_observability.py` | Low-cardinality counters and histograms verified. |
| **Liveness & Readiness Probes** | **PROVEN** | `tests/test_health_readiness.py` | `/api/health` and `/api/ready` verify process and dependency status. |
| **Graceful Shutdown & Task Draining** | **PROVEN** | `tests/test_shutdown.py` | `ShutdownManager` rejects new tasks and drains pending tasks. |
| **Production Configuration Fail-Closed** | **PROVEN** | `tests/test_production_config.py` | Rejects short secrets, wildcard CORS, and bad database configs. |
| **Docker Container Contract** | **PROVEN** | `tests/test_container_contract.py` | Dockerfile syntax, non-root `appuser`, and HEALTHCHECK verified. |
| **Docker Daemon Runtime Execution** | **UNVERIFIED** | Local Docker daemon inactive | Docker binary present (v29.6.1) but daemon was inactive. |
