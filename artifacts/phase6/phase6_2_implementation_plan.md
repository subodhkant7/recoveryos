# Phase 6.2: Sequential Implementation Roadmap

---

## 1. Overview & Execution Protocol
Phase 6.2 implementation is decomposed into 5 sequential, independently verifiable sub-phases. No phase proceeds to the next until its automated tests pass and acceptance criteria are fulfilled.

---

## 2. Sequential Sub-Phase Roadmap

### **Phase 6.2.1: Pub/Sub Message Models & Local Dispatch / Ingestion Contracts**
- **Objective:** Define Pydantic schema for `WorkflowExecutionMessage`, create in-memory/mock Pub/Sub publisher and consumer abstractions, and update `backend/config.py`.
- **Files to Create / Modify:**
  - `[NEW]` [backend/events/message_models.py](../../backend/events/message_models.py)
  - `[NEW]` [backend/events/publisher.py](../../backend/events/publisher.py)
  - `[NEW]` [backend/events/consumer.py](../../backend/events/consumer.py)
  - `[MODIFY]` [backend/config.py](../../backend/config.py)
- **Infrastructure to Create:** None (Local execution).
- **Tests to Add:** `tests/test_pubsub_message_contract.py` (Validates serialization, deserialization, correlation headers, and poison message rejection).
- **Acceptance Criteria:** 100% unit tests pass with zero regression on existing 139 deterministic tests.
- **Rollback Criteria:** Delete new module files; no runtime impact.
- **Production Risk:** **ZERO** (No deployment).

---

### **Phase 6.2.2: Dedicated Worker Execution Engine & Deduplication Layer**
- **Objective:** Decouple `_run_agent` from `server.py` into a reusable `WorkflowWorkerService` that enforces message deduplication, `OperationClaim` lease checks, and Firestore OCC updates.
- **Files to Create / Modify:**
  - `[NEW]` [backend/worker/service.py](../../backend/worker/service.py)
  - `[MODIFY]` [backend/api/server.py](../../backend/api/server.py) (Adds `/api/pubsub/consume` push endpoint)
  - `[MODIFY]` [backend/persistence/workflow_store.py](../../backend/persistence/workflow_store.py) (Adds message deduplication tracking)
- **Infrastructure to Create:** None (Local emulator / in-memory).
- **Tests to Add:** `tests/test_worker_deduplication.py` (Tests at-least-once message redelivery, concurrent duplicate drops, and OCC conflict safety).
- **Acceptance Criteria:** Duplicate Pub/Sub messages drop cleanly without duplicate external mutations.
- **Rollback Criteria:** Revert `server.py` and `workflow_store.py`.
- **Production Risk:** **ZERO** (Local test only).

---

### **Phase 6.2.3: Distributed Gemini Quota Rate Limiter (Cloud Tasks + Leased Window)**
- **Objective:** Implement distributed quota management via Cloud Tasks dispatch pacing and Firestore token document coordination to allow scaling beyond a single worker instance safely.
- **Files to Create / Modify:**
  - `[NEW]` [backend/llm/distributed_limiter.py](../../backend/llm/distributed_limiter.py)
  - `[MODIFY]` [backend/llm/resilience.py](../../backend/llm/resilience.py)
- **Infrastructure to Create:** Cloud Tasks queue `recoveryos-gemini-queue` (in GCP project `recoveryos-506713`).
- **Tests to Add:** `tests/test_distributed_quota_limiter.py` (Simulates 10 concurrent worker tasks and verifies aggregate dispatch rate $\le 15\text{ RPM}$).
- **Acceptance Criteria:** Zero 429 quota exhaustion errors under simulated traffic burst.
- **Rollback Criteria:** Revert `ResilientGemini` to process-local token queue.
- **Production Risk:** **LOW** (Safe serverless queue).

---

### **Phase 6.2.4: GCP Pub/Sub Topic, Subscription & Dead-Letter Provisioning**
- **Objective:** Provision Cloud Pub/Sub topic `recoveryos-workflow-events`, push subscription `recoveryos-worker-sub`, dead-letter topic `recoveryos-workflow-deadletter`, and Cloud Monitoring alert policies.
- **Files to Create / Modify:**
  - `[NEW]` `scripts/provision_pubsub.sh`
- **Infrastructure to Create:** GCP Pub/Sub topics, subscriptions, and dead-letter queues in `asia-east1`.
- **Tests to Add:** `tests/test_live_pubsub_integration.py` (Publishes real message to GCP Pub/Sub and verifies worker consumption).
- **Acceptance Criteria:** Real message round-trip time $< 250\text{ms}$; dead-letter routing verified on poison message.
- **Rollback Criteria:** Delete Pub/Sub subscriptions and topics via `gcloud pubsub`.
- **Production Risk:** **LOW** (Independent parallel infrastructure; does not affect active revision).

---

### **Phase 6.2.5: Cloud Run Dual-Service Deployment & End-to-End Acceptance Gate**
- **Objective:** Deploy updated API and Worker services to Cloud Run, execute end-to-end distributed acceptance tests, and verify distributed tracing and dead-letter alerting.
- **Files to Create / Modify:**
  - `[MODIFY]` [Dockerfile](../../Dockerfile)
- **Infrastructure to Create:** Cloud Run revision update on `recoveryos`.
- **Tests to Add:** `tests/test_production_acceptance_phase6_2.py`.
- **Acceptance Criteria:** End-to-end workflow execution passes with 100% green tests; API response $< 50\text{ms}$.
- **Rollback Criteria:** Revert Cloud Run traffic split back to `recoveryos-00004-sw7` instantly.
- **Production Risk:** **MEDIUM** (Controlled via Knative revision traffic splitting).
