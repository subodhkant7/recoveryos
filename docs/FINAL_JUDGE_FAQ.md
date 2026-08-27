# RecoveryOS — Final Judge FAQ

Technical, implementation-grounded answers to the 15 most important judge questions.

---

### 1. Isn't this just an automated playbook?
**No.** Automation playbooks execute static step sequences with hardcoded assumptions. RecoveryOS is an agentic recovery engine:
- The agent actively reasons about failure signals and plans dynamic alternatives (`backend/engine/agent_runner.py`).
- Deterministic Python code (`PolicyEngine`) enforces policy boundaries and prevents dangerous actions.
- The outcome contract model requires independent outcome verification before completing (`contract.all_verified()`).

---

### 2. Where is the reasoning?
Reasoning takes place within the Gemini ADK agent loop. The agent is provided a prompt hydrated with active failure signals, completed steps, failed steps, and the `OutcomeContract`. It formulates diagnoses and selects alternative recovery tools (e.g., primary gateway down → switch to secondary). Every reasoning step is streamed as an `AGENT_REASONING` timeline event and rendered in the live terminal feed.

---

### 3. How does RecoveryOS decide whether it is allowed to act?
The decision is governed by the deterministic `PolicyEngine` (`backend/engine/policy_engine.py`):
- It evaluates policy rules against tool names, target providers, blast radius limits, and customer constraints.
- If policy checks pass with 0 constraint violations, the action is marked `AUTONOMOUS ACTION PERMITTED`.
- If a constraint is violated or risk exceeds thresholds, the system halts at `WorkflowState.AWAITING_APPROVAL`.

---

### 4. What happens when evidence conflicts?
In Scenario 02 (`contradictory_evidence`), multiple identity/risk bureaus return conflicting data (e.g., risk scores of 42 vs 88). Because autonomous action under contradictory evidence would violate compliance rules, the engine refuses to guess. It transitions to `AWAITING_APPROVAL`, exposes an approval card on the dashboard, and waits for an authenticated human operator.

---

### 5. How do you prove recovery actually happened?
Through independent outcome probes. After executing a tool, the agent runner transitions to `WorkflowState.VERIFYING`. It issues an active verification probe (e.g., an HTTP probe against the secondary billing provider). Only when all required outcomes are marked `verified: true` does the system transition to `COMPLETED` and generate the **Recovery Proof Certificate**.

---

### 6. Why isn't tool execution considered recovery?
Because **Action Executed ≠ Recovery Verified**. In production systems, an API call returning HTTP 200 does not guarantee that data was persisted correctly, downstream microservices synchronized, or payments are functioning. RecoveryOS treats tool execution as a tentative mutation and insists on independent outcome verification.

---

### 7. What prevents duplicate execution?
Every mutating tool invocation generates a deterministic idempotency key (`workflow_id:step_id:attempt_count`). The persistence layer (`backend/persistence/workflow_store.py:save_idempotency_record`) records every operation. If a duplicate command arrives or a worker resumes, the system recognizes the existing key and returns the cached result without executing a second external mutation.

---

### 8. What happens if the worker crashes?
RecoveryOS uses Optimistic Concurrency Control (OCC) and operation claim leases. Mutating operations acquire a 60-second lease with worker ownership. If the worker process dies mid-flight:
- The lease expires.
- When the workflow is picked up by a replacement worker, `reconcile_interrupted_workflow()` inspects external ground truth.
- If the external side-effect succeeded before the crash, the step is completed; if not, it safely resumes.

---

### 9. Can a human intervene?
**Yes.** The Operator Control Plane provides multiple intervention mechanisms:
- Human approval endpoints (`POST /api/workflows/{id}/approve/{id}`) allowing human approvers to authorize or reject recovery plans.
- Manual workflow cancellation (`POST /api/workflows/{id}/cancel`) transitioning running workflows to `ESCALATED`.
- Manual workflow recovery dispatch (`POST /api/workflows/{id}/recover`).

---

### 10. Can replay mutate production state?
**No.** Decision Replay (`↺ DECISION REPLAY • READ-ONLY`) is strictly read-only. It replays historical event arrays already recorded in `snapshot.events`. The replay functions in `app.js` (`startReplay`, `stepReplay`, `pauseReplay`, `resetReplay`) contain zero `fetch`, `apiFetch`, or HTTP `POST` calls.

---

### 11. What happens if verification fails?
If an outcome verification probe fails or returns invalid evidence, `agent_runner.py` detects that `contract.all_verified()` is `False`. The workflow engine transitions the workflow to `WorkflowState.RECOVERING` (not `COMPLETED`), increments the recovery attempt counter, and prompts the agent to attempt an alternate path or escalate.

---

### 12. What prevents hallucinated recovery claims?
The LLM does not own the state transition to `COMPLETED`. Deterministic Python application code in `WorkflowEngine` controls the state machine (`VALID_TRANSITIONS`). Direct transition from `EXECUTING` to `COMPLETED` is impossible in the state machine (raises `InvalidTransitionError`). Only deterministic verification code can transition to `COMPLETED`.

---

### 13. What parts are real versus simulated?
- **Real**: FastAPI backend, JWT authentication, RBAC authorization, Pydantic data models, deterministic state machine, OCC concurrency control, Pub/Sub event publishers, SSE ticket generation and streaming, and frontend event machine.
- **Simulated**: External SaaS endpoints (Stripe, Adyen, Experian, Equifax) run in high-fidelity simulated services (`backend/simulation/external_services.py`) to allow controllable, deterministic failure injection during hackathon evaluation.

---

### 14. What is the biggest limitation of the current implementation?
In unmocked live Gemini environments, unthrottled concurrent API calls can encounter Google Cloud project rate limits (HTTP 429). The system includes an in-memory execution mode and exponential backoff retry policies to guarantee reliable demonstration.

---

### 15. How would this scale to real infrastructure?
In production, the simulated service layer is replaced with real SDK clients (e.g., Stripe SDK, Cloud SQL, Kubernetes client). The worker process deploys as a horizontally autoscaling Cloud Run worker consuming events from Google Cloud Pub/Sub, with state stored in Cloud Firestore.
