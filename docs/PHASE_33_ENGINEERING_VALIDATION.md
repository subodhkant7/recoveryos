# RecoveryOS Engineering Validation

This document records implementation-backed validation of RecoveryOS's key reliability, recovery, verification, security, and governance behaviors.

---

### Q1: "Is this just an automation playbook with a fancy UI?"
**Answer**: **No.** Automation playbooks execute static command sequences assuming success. RecoveryOS is an autonomous agent with a deterministic policy and outcome verification harness:
- The agent actively correlates failure signals and formulates hypotheses (`backend/agents/agent_factory.py`).
- Deterministic code (`PolicyEngine` in `backend/engine/policy_engine.py`) enforces hard constraints and stops unsafe automated actions.
- The workflow engine enforces the invariant **Action Executed ≠ Recovery Verified**. No workflow can complete without independent outcome verification (`agent_runner.py:217-228`).

---

### Q2: "Where does the reasoning happen?"
**Answer**: Reasoning occurs within the Gemini ADK agent loop (`backend/engine/agent_runner.py`). The agent receives an initial prompt hydrated with the live `OutcomeContract`, active failure signals, previously failed steps, and completed steps. It reasons about alternative paths (e.g., primary gateway Stripe down → fail over to secondary gateway Adyen). Every reasoning step is emitted as an `AGENT_REASONING` timeline event and rendered in the live terminal feed.

---

### Q3: "How do you know the action actually worked?"
**Answer**: The action tool execution only records a step result. Recovery is verified independently:
- Every scenario defines a required outcome with an explicit `verification_method` in its `OutcomeContract` (`backend/simulation/scenarios.py`).
- Upon tool completion, the agent runner transitions to `WorkflowState.VERIFYING` and triggers an independent outcome probe (e.g., active billing subscription probe).
- The workflow transitions to `COMPLETED` **strictly** if `contract.all_verified()` evaluates to true. If verification fails, it transitions to `RECOVERING`, never `COMPLETED`.

---

### Q4: "What happens if the evidence conflicts?"
**Answer**: The policy engine enforces bounded autonomy:
- In Scenario 02 (`contradictory_evidence`), the failure injector returns conflicting risk/billing evidence across providers.
- The policy engine detects that automated failover violates risk constraints.
- Execution immediately halts, the workflow transitions to `WorkflowState.AWAITING_APPROVAL`, and an `APPROVAL_REQUIRED` event is dispatched.
- The UI exposes an authenticated human approval card. The agent cannot resume execution until an authorized human (`APPROVER` or `ADMIN` role) explicitly signs off.

---

### Q5: "What happens if the worker crashes halfway through?"
**Answer**: RecoveryOS provides durable recovery via Optimistic Concurrency Control (OCC) and operation lease expiration:
- Every mutating step requires an operation claim lease (e.g., 60s TTL) with an idempotency key (`backend/persistence/workflow_store.py`).
- If a worker container dies mid-flight, its lease expires.
- When the workflow restarts or is picked up by another worker, `reconcile_interrupted_workflow()` inspects external service ground truth before resuming.
- If the external mutation already succeeded, the step is marked complete without re-executing; if it failed, it resumes safely.

---

### Q6: "Can the AI accidentally execute the same recovery twice?"
**Answer**: **No.** Every external mutation tool generates a deterministic idempotency key (`workflow_id:step_id:attempt_count`). The idempotency layer (`backend/persistence/workflow_store.py:save_idempotency_record`) deduplicates operations. If a tool is called with an existing idempotency key, it returns the cached result rather than triggering a second external mutation (e.g., preventing double billing).

---

### Q7: "Can you show me that recovery actually completed?"
**Answer**: **Yes.** The **Evidence-Backed Recovery Proof** appears dynamically on the console canvas. It renders:
- **Incident Type**: The failure diagnosed.
- **Action Taken**: The sanitized recovery tool executed.
- **Verification Evidence**: The live verification probe result (e.g., `HTTP 200`).
- **Operator Interventions**: Derived directly from the `snapshot.approvals` record.
- **MTTR**: Dynamically calculated from authoritative timestamps (`wf.created_at` → `wf.completed_at`).
- **Outcome Contract**: `✓ FULFILLED`.
The proof is guarded and will **never** render for uncompleted, failed, active, or internally inconsistent workflows.

---

### Q8: "Is the demo using fake telemetry?"
**Answer**: **No.** Every event in the UI originates from real Server-Sent Events (SSE) backed by `WorkflowStore` event logs.
- Clicking "⚡ SIMULATE AN INCIDENT" makes a live `POST /api/scenarios/{scenario}` call.
- The client connects to `/api/workflows/{id}/events/stream` using single-use cryptographic tickets (`sset_...`).
- The graph animations, terminal logs, and inspector cards update in real time as normalized SSE event packets arrive.

---

### Q9: "Can an operator override the AI?"
**Answer**: **Yes.** The platform provides full administrative control plane capabilities:
- An operator can gracefully cancel any running workflow via `POST /api/workflows/{id}/cancel`, transitioning it to `ESCALATED` with an immutable audit record.
- An operator can reject a pending human approval, immediately halting the workflow in `ESCALATED` state.
- An operator can manually trigger workflow recovery via `POST /api/workflows/{id}/recover`.

---

### Q10: "What prevents the AI from declaring success prematurely?"
**Answer**: The state machine architecture. The LLM agent does not own the state transition to `COMPLETED`.
- Deterministic Python application code in `WorkflowEngine` controls state transitions (`backend/models/workflow.py:VALID_TRANSITIONS`).
- Direct transition from `EXECUTING` to `COMPLETED` is physically impossible in the state machine (raises `InvalidTransitionError`).
- The transition to `COMPLETED` can only occur from `VERIFYING` after `contract.all_verified()` succeeds.
