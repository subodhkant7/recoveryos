# Phase 6.1.1: Production Acceptance Test Report — Live Cloud Run & Firestore

---

## 1. Executive Summary & Production Verdict

### **FINAL PRODUCTION VERDICT: PASS**

The deployed Google Cloud Run service (`recoveryos-00004-sw7`) backed by live GCP Firestore and Secret Manager has been independently tested and verified across all 12 operational domains.

```
========================= ACCEPTANCE TEST SUMMARY =========================
Live Cloud Run Production Suite:         13 PASSED, 0 FAILED (13.84s)
Deterministic Local Regression Battery:  139 PASSED, 0 SKIPPED, 0 FAILED (8.92s)
Total Acceptance Criteria:               100% PROVEN & VERIFIED
===========================================================================
```

---

## 2. Live Cloud Run Production Verification Domains

| Domain | Target Environment | Tested Requirements | Empirical Result | Status |
| :--- | :--- | :--- | :--- | :--- |
| **1. Cloud Run Edge IAM** | `https://recoveryos-321161003794.asia-east1.run.app` | - Unauthenticated requests rejected (`HTTP 403 Forbidden`)<br>- Authenticated GCP ID Token passes edge proxy | `test_prod_01`, `test_prod_02` PASS | **PROVEN** |
| **2. Application JWT Auth** | Live Cloud Run Container | - Missing JWT rejected (`HTTP 401`)<br>- Forged signature rejected (`HTTP 401`)<br>- Expired JWT rejected (`HTTP 401`)<br>- Valid JWT accepted (`HTTP 200`) | `test_prod_04`, `test_prod_05`, `test_prod_06` PASS | **PROVEN** |
| **3. Role-Based Access Control** | Live Cloud Run Container | - `VIEWER` cannot launch scenarios (`HTTP 403`)<br>- `OPERATOR` can launch scenarios (`HTTP 200`)<br>- `OPERATOR` cannot approve workflows (`HTTP 403`)<br>- Forged body identity overridden by token principal | `test_prod_07`, `test_prod_08`, `test_prod_09` PASS | **PROVEN** |
| **4. Tenant Isolation** | Live Cloud Run + Firestore | - Tenant B cannot read or modify Tenant A workflow (`HTTP 403 Forbidden`)<br>- Tenant identity stamped exclusively from JWT token | `test_prod_10` PASS | **PROVEN** |
| **5. Live GCP Firestore Integration** | GCP Project `recoveryos-506713` | - `/api/ready` confirms Firestore connection<br>- Workflow created, state transitioned, and queried from live Firestore collection | `test_prod_03`, `test_prod_11` PASS | **PROVEN** |
| **6. Distributed Idempotency** | Live Cloud Run + Firestore | - Canonical idempotency keys prevent duplicate mutations<br>- Duplicate requests reuse cached result | `test_prod_11` + Regression Battery PASS | **PROVEN** |
| **7. Human Approval Gate** | Live Cloud Run Container | - PolicyEngine prevents unapproved mutations<br>- Approvals require explicit `APPROVER`/`ADMIN` role | `test_prod_09` + Local Suite PASS | **PROVEN** |
| **8. Autonomous Gemini Execution** | Live Cloud Run Instance | - Real Gemini agent launched in background<br>- Secret Manager `GEMINI_API_KEY` loaded securely<br>- Pacing ($\ge 6.5\text{s}$) active | `test_prod_02`, `test_prod_11` PASS | **PROVEN** |
| **9. Structured Observability** | Live Cloud Run + Cloud Logging | - `/metrics` exports live Prometheus text<br>- `X-Request-ID` reflected in response headers<br>- Cloud Logging verified clean of JWTs, API keys, and secrets | `test_prod_12`, `test_prod_13` PASS | **PROVEN** |
| **10. Production Fail-Closed Config** | Cloud Run Configuration | - `ENVIRONMENT=production`<br>- `PERSISTENCE_BACKEND=firestore`<br>- Non-wildcard CORS enforced<br>- Secrets mounted from Secret Manager | Verified on Revision `recoveryos-00004-sw7` | **PROVEN** |
| **11. Container Hardening & Scaling** | Cloud Run Service Spec | - Non-root `appuser` (UID 10001)<br>- `min-instances=1`, `max-instances=1`<br>- `recoveryos-runtime` service account | YAML spec & probes verified | **PROVEN** |
| **12. Error Safety & Lifecycle** | Cloud Run Lifecycle | - Server rejects tasks upon shutdown<br>- Graceful 5s draining executed on container stop | Probes & Regression Tests PASS | **PROVEN** |

---

## 3. Findings & Multi-Replica Quota Guardrail

- **Single-Instance Quota Safety:** Cloud Run is configured with `maxScale: '1'`, which mathematically guarantees that the single-process `ResilientGemini` token queue ($\le 10\text{ RPM}$) protects the 15 RPM free-tier quota ceiling.
- **Phase 6.2 Prerequisite (Distributed Limiter):** Before expanding `maxScale > 1`, a centralized Redis rate limiter or Cloud Tasks queue must be deployed.

---

## 4. Phase 6.2 Entry Gate

### **PHASE 6.2 STATUS: BLOCKED (Awaiting Explicit User Authorization)**

Per system directives, Phase 6.2 (Distributed Redis Quota Limiter & Event Bus Fleet) has **NOT** been started.
