# RecoveryOS

> **Autonomous infrastructure recovery that proves the outcome.**
> 
> *When enterprise infrastructure fails, RecoveryOS doesn't just execute a playbook. It observes failure signals, reasons about root causes, executes recovery tools within policy boundaries, independently verifies outcomes, and generates an auditable Recovery Proof.*

---

## 1. What It Is

RecoveryOS is an autonomous operations and incident recovery engine built on Google Cloud, Gemini, and the Google Agent Development Kit (ADK). It continuously monitors infrastructure dependencies, correlates multi-provider telemetry, determines compliant recovery actions using deterministic policy engines, executes idempotent recovery tools, and verifies business outcomes before marking systems recovered.

---

## 2. The Problem

Traditional automation runs static scripts or playbooks when incidents occur. If a script executes with exit code 0, the automation system assumes success—even if the underlying database remains corrupted, downstream billing is broken, or failover created duplicate subscriptions.

In production:
- **Action Executed ≠ Recovery Verified**: Running `switch_payment_gateway()` does not mean payments are functioning.
- **Unbounded Autonomy Risks Catastrophe**: An autonomous agent guessing during ambiguous or contradictory incidents can violate compliance and corrupt state.
- **Worker Crashes Cause Duplication**: An automation process crashing mid-operation often double-bills customers or leaves dangling leases.

---

## 3. How It Works: The 5-Stage Recovery Lifecycle

RecoveryOS executes through five deterministic lifecycle stages:

```
[01 DETECT] ───► [02 REASON] ───► [03 ACT] ───► [04 VERIFY] ───► [05 RECOVERED]
   Signal         Policy &        Idempotent      Independent       Recovery Proof
  Observed        Autonomy        Tool Action     Outcome Probe       Certificate
```

1. **`01 DETECT`**: Observes telemetry signals (e.g., consecutive HTTP 500 timeouts on primary payment gateways).
2. **`02 REASON`**: Correlates failure signatures, evaluates deterministic policy constraints (allowed tools, rate limits, risk thresholds), and decides whether autonomous action is authorized.
3. **`03 ACT`**: Executes idempotent recovery actions (e.g., failover to secondary provider Adyen) with Optimistic Concurrency Control (OCC) operation claims.
4. **`04 VERIFY`**: Triggers independent outcome verification probes (e.g., active subscription probe returning HTTP 200). The agent cannot transition to `COMPLETED` without fulfilling the `OutcomeContract`.
5. **`05 RECOVERED`**: Issues a cryptographically bound **Recovery Proof Certificate** with authoritative MTTR, operator intervention records, and contract verification status.

---

## 4. Architectural Invariants

- **Action ≠ Recovery**: The state machine strictly forbids direct transitions from `EXECUTING` to `COMPLETED`. All executions must pass through the `VERIFYING` outcome gate.
- **Bounded Autonomy**: When evidence is contradictory or risk thresholds are exceeded, RecoveryOS refuses to guess. It halts at `AWAITING_APPROVAL` and escalates to an authenticated human approver.
- **Durable Worker Resilience**: Distributed operation claims use Optimistic Concurrency Control (OCC) leases. If a worker container crashes mid-execution, replacement workers reconcile external state before safely resuming without double execution.
- **Read-Only Decision Replay**: Historical decision sequences can be replayed deterministically (`PLAY`, `STEP`, `PAUSE`, `RESET`) with zero network mutation calls.

---

## 5. Three Hackathon Demo Scenarios

| Scenario | Failure Mode | Autonomous Behavior | Expected Outcome |
|:---|:---|:---|:---|
| **01. Billing Outage** | Primary payment provider (Stripe) experiences HTTP 503 outage. | Detects failure, evaluates policy (0 violations), executes failover to Adyen, verifies via live subscription probe. | Autonomous resolution (`MTTR ~5.2s`, `Interventions: 0`). Recovery Proof generated. |
| **02. Contradictory Evidence** | Credit and identity bureaus return conflicting risk scores (42 vs 88). | Identifies conflicting evidence, detects policy violation for autonomous failover, halts at `AWAITING_APPROVAL`. | Human operator reviews audit card, signs off with `APPROVE` or `REJECT`. |
| **03. Worker Interruption** | Worker process terminates mid-mutation. | OCC lease expires (60s). Replacement worker reconciles state against external services, resumes idempotently. | Resilient recovery with evidence-gated badges (`✓ NO DUPLICATE EXECUTION`, `✓ NO DOUBLE BILLING`). |

---

## 6. Local Setup & Quickstart

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

## 7. Running the Test Suite

Execute the complete regression test suite:
```bash
source .venv/bin/activate
python -m pytest tests/ -v --tb=short --ignore=tests/test_distributed_gemini_quota.py
```

### Targeted Test Suites:
- **Judge Attack Suite**: `pytest tests/test_phase33_final_judge_attack.py -v`
- **Demo Attack Suite**: `pytest tests/test_phase32_demo_attack.py -v`
- **Adversarial Audit Suite**: `pytest tests/test_phase31_adversarial_audit.py -v`
- **Integration Suite**: `pytest tests/test_phase30_final_integration.py -v`

---

## 8. Live Judge Demo Script (2.5 Minutes)

Refer to [`docs/PHASE_33_FINAL_JUDGE_SCRIPT.md`](docs/PHASE_33_FINAL_JUDGE_SCRIPT.md) for the timed presentation flow:
1. **0:00 - 0:20**: Problem statement & "Action ≠ Recovery" thesis.
2. **0:20 - 1:10**: Launch **Scenario 01 (Billing Outage)** → Show 5-stage progression → Inspect 4-Questions card → Show Recovery Proof.
3. **1:10 - 1:50**: Launch **Scenario 02 (Contradictory Evidence)** → Show autonomy boundary halt → Approve as operator.
4. **1:50 - 2:30**: Launch **Scenario 03 (Worker Interruption)** → Show OCC lease expiry → Idempotent reconciliation without double billing.

---

## 9. Security & Access Control

- **Role-Based Access Control (RBAC)**: Enforces `VIEWER`, `OPERATOR`, `APPROVER`, and `ADMIN` roles on all administrative routes.
- **Single-Use SSE Tickets**: Streaming endpoints require single-use, cryptographically random tickets (`sset_...`) with a 60-second TTL to eliminate JWT leakage in server logs.
- **Zero Embedded Credentials**: Static frontend assets contain zero API keys, secrets, or bearer tokens.

---

## 10. Current Limitations & Scope

- **Simulated External Providers**: In development and demo environments, external billing and verification endpoints (Stripe, Adyen, Experian, Equifax) use deterministic high-fidelity simulation layers to allow reproducible failure injection.
- **Rate Limits on Live Cloud Gemini**: Live unmocked external Gemini calls depend on project quota; local in-memory execution mode is used for deterministic, instant judge evaluation.
