# RecoveryOS

> **A recovery-first control plane for autonomous operations.**
> 
> *RecoveryOS governs autonomous operations with explicit autonomy boundaries and never declares recovery successful until the outcome has been independently verified.*
> 
> **Core Invariant:** `Action Executed ≠ Recovery Verified` • **Autonomy Principle:** `Autonomy is governed, not assumed.`

---

## 1. Executive Summary & Core Value Proposition

Traditional automation runs static playbooks (`DETECT → RUN PLAYBOOK`). If an automated script executes with exit code 0, the automation system assumes success—even if the underlying database remains corrupted, downstream billing is broken, or failover created duplicate subscriptions.

**RecoveryOS treats verification as part of recovery itself.** When infrastructure fails, RecoveryOS observes failure signals, reasons about root causes, executes recovery tools within explicit policy boundaries, independently verifies outcomes, and generates an auditable Recovery Proof Certificate.

---

## 2. Architecture & Google Cloud Integration

```
+───────────────────────────────────────────────────────────────────────────────────+
|                         RECOVERYOS RECOVERY CONTROL LOOP                          |
|                                                                                   |
|    Telemetry Failure Signal (e.g. Stripe 500 Outage / Contradictory Bureaus)      |
|                                     │                                             |
|                                     ▼                                             |
|    [01 DETECT] ───► Cloud Pub/Sub Telemetry Ingestion (topic: workflow-events)    |
|                                     │                                             |
|                                     ▼                                             |
|    [02 REASON] ───► Gemini 1.5 Pro / Google ADK Agent (Formulates Diagnosis)      |
|                                     │                                             |
|                                     ▼                                             |
|    AUTONOMY POLICY BOUNDARY (Deterministic Python PolicyEngine)                   |
|             │                                              │                      |
|     [Policy Permits]                               [Policy Conflict]              |
|             │                                              │                      |
|             ▼                                              ▼                      |
|    [03 ACT] Idempotent Tool Dispatch               [AWAITING_APPROVAL]            |
|             │ (OCC Lease 60s)                      Human Authorization Gate       |
|             ▼                                              │                      |
|    Tentative External Side-Effect                          ▼                      |
|             │                                     Operator Approve / Reject       |
|             ▼                                                                     |
|    [04 VERIFY] Independent Outcome Probe (Active HTTP Verification Query)         |
|             │                                                                     |
|             ▼                                                                     |
|    OutcomeContract.all_verified() == True                                         |
|             │                                                                     |
|             ▼                                                                     |
|    [05 RECOVERED] Issue Cryptographically Bound Recovery Proof Certificate        |
+───────────────────────────────────────────────────────────────────────────────────+
```

### Google Cloud Components in Use:
- **Cloud Run**: Hosts the FastAPI asynchronous Control Plane API and Operator Console (`backend/api/server.py`).
- **Cloud Pub/Sub**: Event bus for asynchronous event ingestion, telemetry streaming, and worker dispatch (`backend/events/publisher.py`).
- **Cloud Firestore**: Persistent store for workflow state, OCC leases, idempotency records, and audit logs (`backend/persistence/workflow_store.py`).
- **Gemini 1.5 Pro & Google ADK**: Powers agentic reasoning, failure correlation, and alternative planning (`backend/agents/agent_factory.py`, `backend/engine/agent_runner.py`).

---

## 3. Direct Source Code Pointers for Judges

| Component | File Path | Architectural Responsibility |
|:---|:---|:---|
| **State Machine & Invariants** | [`backend/models/workflow.py`](backend/models/workflow.py) | `VALID_TRANSITIONS` forbids `EXECUTING → COMPLETED`. Enforces outcome contract models. |
| **Autonomy Boundary** | [`backend/engine/policy_engine.py`](backend/engine/policy_engine.py) | Deterministic Python rules enforcing tool permissions, blast radius, and approval thresholds. |
| **Agent Runner & Verification Gate** | [`backend/engine/agent_runner.py`](backend/engine/agent_runner.py) | Executes ADK loop, transitions to `VERIFYING`, probes outcome, and enforces `contract.all_verified()`. |
| **Durable OCC Persistence** | [`backend/persistence/workflow_store.py`](backend/persistence/workflow_store.py) | Optimistic Concurrency Control, 60s worker leases, step idempotency deduplication. |
| **Control Plane Server & SSE** | [`backend/api/server.py`](backend/api/server.py) | FastAPI server, RBAC authorization, single-use SSE ticket minting, operator endpoints. |
| **Interactive Command Center** | [`backend/api/static/app.js`](backend/api/static/app.js) | Authoritative single-state event machine, read-only replay engine, live inspector. |
| **Simulation & Failure Injection** | [`backend/simulation/external_services.py`](backend/simulation/external_services.py) | High-fidelity simulated third-party services (Stripe, Adyen, Experian, Equifax). |

---

## 4. What Is Real vs. What Is Simulated

- **Real & Deterministic**:
  - Full FastAPI async API server with OpenAPI docs (`/docs`).
  - Strict Python state machine enforcing `Action ≠ Recovery`.
  - Deterministic `PolicyEngine` governing autonomous vs human-gated decisions.
  - Optimistic Concurrency Control (OCC) leases and step-level idempotency stores.
  - Google ADK agent integration with Gemini 1.5 Pro prompt reasoning loops.
  - Real single-use SSE ticket streaming and browser event pipeline.
  - Complete 377-test regression suite.
- **Simulated for Hackathon Evaluation**:
  - External SaaS endpoints (Stripe, Adyen, Experian, Equifax) run in a high-fidelity local simulation layer (`backend/simulation/external_services.py`) to provide reproducible, instant, zero-cost failure injection without requiring live third-party bank accounts.

---

## 5. Three Demonstration Scenarios

| Scenario | Capability | Story & Strategic Purpose | Expected Outcome |
|:---|:---|:---|:---|
| **01. Billing Provider Outage** | **Autonomous Recovery** | Primary payment gateway (Stripe) encounters HTTP 500 timeouts → evidence correlated → policy evaluated (0 violations) → automated failover to Adyen → verified via active subscription probe. *Proves autonomous recovery when policy permits.* | Autonomous resolution (`MTTR ~5.2s`, `Interventions: 0`). Recovery Proof generated. |
| **02. Contradictory Evidence** | **Bounded Autonomy** | Conflicting risk scores (42 vs 88) from Experian and Equifax → policy constraint violated → autonomy boundary reached → autonomous execution safely blocked. *Proves the system knows when NOT to act.* | Safe halt at `AWAITING_APPROVAL`. Operator authorizes or rejects with audit trace. |
| **03. Worker Interruption** | **Resilient Execution** | Worker container killed mid-mutation → OCC lease expires (60s) → replacement worker reconciles state against external service → resumes idempotently. *Proves recovery mechanism survives crash.* | Safe recovery with evidence-gated badges (`✓ NO DUPLICATE EXECUTION`, `✓ NO DOUBLE BILLING`). |

---

## 6. Recommended 4-Minute Judge Demo Sequence

Refer to [`docs/PHASE_33_FINAL_JUDGE_SCRIPT.md`](docs/PHASE_33_FINAL_JUDGE_SCRIPT.md) for the timed presentation flow:

1. **0:00 - 0:30 (The Problem & Central Thesis)**:
   - Introduce the core invariant: **Action Executed ≠ Recovery Verified**.
   - Explain why autonomous operations require a recovery control plane with explicit autonomy boundaries.
2. **0:30 - 1:45 (Scenario 01: Autonomous Recovery)**:
   - Click `⚡ SIMULATE AN INCIDENT` → Choose **Billing Provider Outage** → Click `⚡ RUN AUTONOMOUS RECOVERY`.
   - Watch the 5-stage lifecycle graph: `01 DETECT` → `02 REASON` → `03 ACT` → `04 VERIFY` → `05 RECOVERED`.
   - Inspect the **"Why Did You Do That?"** decision trace (Questions 01–04).
   - Review the **Recovery Proof Certificate**: Highlight that recovery was proved by an independent probe, not assumed from tool execution.
3. **1:45 - 2:45 (Scenario 02: Bounded Autonomy & Refusal to Guess)**:
   - Click `⚡ SIMULATE AN INCIDENT` → Choose **Contradictory Evidence** → Click `⚡ TEST AUTONOMY BOUNDARY`.
   - Show that execution **halts** at `AUTONOMY BOUNDARY REACHED: HUMAN APPROVAL REQUIRED` due to conflicting bureau scores.
   - Click `✓ AUTHORIZE RECOVERY ACTION` to demonstrate operator sign-off and state continuation.
4. **2:45 - 3:30 (Scenario 03: Worker Resilience & Lease Replay)**:
   - Click `⚡ SIMULATE AN INCIDENT` → Choose **Worker Interruption** → Click `⚡ TEST RESILIENCE`.
   - Show OCC lease expiry and state reconciliation without double billing.
   - Demonstrate `↺ DECISION REPLAY` (Play / Step / Reset) in read-only mode.
5. **3:30 - 4:00 (Conclusion & Judge Takeaway)**:
   - Emphasize: *RecoveryOS is not another agent that performs an action; it is a control plane that governs autonomous action and refuses to call recovery successful until the outcome is independently verified.*

---

## 7. Local Setup & Quickstart

### Prerequisites
- Python 3.11+
- Virtual environment (`venv`)

### Installation
```bash
# Clone the repository
git clone https://github.com/subodhkant7/recoveryos.git
cd recoveryos

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies in development mode
pip install -e ".[dev]"
```

### Starting the Server
```bash
source .venv/bin/activate
uvicorn backend.api.server:app --host 127.0.0.1 --port 8000
```
Open your browser to:
- **Operator Command Center**: `http://localhost:8000/console/`
- **Interactive OpenAPI Documentation**: `http://localhost:8000/docs`

---

## 8. Running the Automated Test Suite

Execute the complete regression test suite:
```bash
source .venv/bin/activate
python -m pytest tests/ -v --tb=short --ignore=tests/test_distributed_gemini_quota.py
```

### Targeted Judge Attack Suites:
```bash
pytest tests/test_phase33_final_judge_attack.py -v
pytest tests/test_phase32_demo_attack.py -v
```

---

## 9. Security & Access Control

- **Role-Based Access Control (RBAC)**: Enforces `VIEWER`, `OPERATOR`, `APPROVER`, and `ADMIN` permissions on all administrative endpoints.
- **Single-Use SSE Tickets**: Streaming endpoints require single-use, cryptographically random tickets (`sset_...`) with a 60-second TTL to eliminate JWT leakage in server logs.
- **Zero Embedded Credentials**: Scanned with 0 API keys, JWT secrets, or private keys across all static assets.

---

## 10. Current Limitations & Scope

- **Synthetic Incident Data**: External third-party billing and verification endpoints use deterministic simulated services (`backend/simulation/external_services.py`) to allow reproducible failure injection during evaluation.
- **Live Cloud Gemini Rate Limits**: Live unmocked external Gemini calls depend on project quota; local in-memory execution mode is used for deterministic, instant judge evaluation.
