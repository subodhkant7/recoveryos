# RecoveryOS Deployment Safety & Progressive Delivery Policy

This document defines the non-negotiable progressive delivery pipeline and safety invariants for deploying updates to the RecoveryOS distributed system on Google Cloud Platform.

---

## 1. Core Progressive Delivery Sequence

```text
1. Local Development & Automated Tests
      ↓
2. Full Local Regression Suite (315+ passing tests)
      ↓
3. Immutable Container Build & Provenance Hash in Artifact Registry
      ↓
4. Deployment to Isolated Staging Microservices (recoveryos-stage & recoveryos-worker-stage)
      ↓
5. Live Staging End-to-End & Stress Verification (scripts/verify_phase22_worker_canary.py)
      ↓
6. Production Canary (Reversible 0% tag or dedicated traffic split)
      ↓
7. Controlled 100% Production Promotion
      ↓
8. Post-Cutover Verification & Monitoring Window (scripts/verify_phase24_observability.py)
      ↓
9. Rollback Readiness Retention (Keep previous revision active with 0% traffic)
```

---

## 2. Why Worker Upgrades Require Isolated Canaries

In Google Cloud Run with Pub/Sub Push Subscriptions:
- A Pub/Sub Push Subscription forwards 100% of messages directly to a single configured `pushEndpoint` URL (`https://<service-url>/`).
- Push subscriptions **cannot perform percentage-based traffic splits** across two distinct Cloud Run services.
- If a new worker revision is deployed to production without prior validation, all incoming workflow executions are immediately routed to that revision.
- **Mandatory Safety Rule**: Every worker update must first undergo a full isolated canary test on the dedicated staging topic (`recoveryos-workflow-execution-stage`) and staging worker (`recoveryos-worker-stage`) before updating the production worker service.

---

## 3. Deployment Invariants & Rollback Rules

1. **Immutable Artifacts**: Never deploy mutable tags (e.g. `:latest`) to production. Always deploy specific SHA256 image digests.
2. **Preserve Rollback Revisions**: Never delete previous production revisions (`recoveryos-00008-2bt`, `recoveryos-worker-00008-5pv`). Retain them as instant fallback targets.
3. **Fail-Closed Security**: Client credentials must be verified with PBKDF2-HMAC-SHA256, role/tenant privileges must be assigned server-side, and SSE streams must require single-use tickets.
4. **Instant Rollback Threshold**: If 5xx errors > 0.1% or push failures occur post-cutover, execute instant traffic rollback via Cloud Run traffic management within 5 seconds.
