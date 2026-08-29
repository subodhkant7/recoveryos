# RecoveryOS — Phase 39 Final Demo Recording Script

> **Target Duration: 3 minutes 50 seconds (Target Window: 3:45 – 3:55)**  
> **Hard Limit: Must not exceed 4:00**  
> **Speaker Style: Technical Founder / Lead Systems Engineer (Measured, Direct, Authoritative)**

---

## Central Thesis & Invariants
- **Core Invariant:** `Agent Action Executed ≠ Recovery Verified`
- **Autonomy Principle:** `Autonomy is governed, not assumed.`
- **Recovery Principle:** `Recovery is only declared successful after independent verification.`

---

## Click-by-Click Timed Demonstration Flow

```
+───────────────────+───────────────────────────────────────────────────────────────────+
| TIMING            | SECTION                                                           |
+───────────────────+───────────────────────────────────────────────────────────────────+
| 0:00 – 0:20       | 1. The Problem & Central Thesis                                   |
| 0:20 – 0:35       | 2. The 5-Stage Recovery Control Loop                              |
| 0:35 – 1:30       | 3. Hero Scenario 01: Billing Provider Outage (Autonomous Recovery)|
| 1:30 – 1:50       | 4. Recovery Proof & Independent Verification Probe                 |
| 1:50 – 2:35       | 5. Scenario 02: Contradictory Evidence (Bounded Autonomy)         |
| 2:35 – 3:10       | 6. Scenario 03: Worker Interruption (Resilient OCC Recovery)      |
| 3:10 – 3:35       | 7. Google Cloud Architecture & Code Tour                          |
| 3:35 – 3:55       | 8. Conclusion & Core Takeaway                                     |
+───────────────────+───────────────────────────────────────────────────────────────────+
```

---

### [0:00 – 0:20] 1. The Problem & Central Thesis
- **Initial Screen**: Operator Command Center (`http://localhost:8000/console/`) in fullscreen.
- **Visual Focus**: Hero ribbon headline: `AUTONOMOUS OPERATIONS NEED A RECOVERY CONTROL PLANE`.
- **Narration**:
  > "Autonomous agents are entering production operations. But when infrastructure fails, today's agents run a script, get an exit code 0, and assume the job is done.
  > 
  > In production, that is catastrophic. Executing an action does not mean the system recovered.
  > 
  > **RecoveryOS is a recovery-first control plane for autonomous operations.** It separates action from recovery, enforces explicit autonomy boundaries, and refuses to declare recovery until the outcome has been independently verified."

---

### [0:20 – 0:35] 2. The 5-Stage Recovery Control Loop
- **Visual Action**: Hover cursor across the 5-stage pipeline on screen:
  `01 DETECT` → `02 REASON` → `03 ACT` → `04 VERIFY` → `05 RECOVERED`.
- **Narration**:
  > "Rather than static playbooks, RecoveryOS executes a 5-stage deterministic lifecycle:
  > Signal detection, agentic reasoning with Gemini 3.5 Flash and ADK, policy-bounded action, an independent outcome verification probe, and an evidence-backed Recovery Proof.
  > 
  > Let's see this in action."

---

### [0:35 – 1:30] 3. Hero Scenario 01: Billing Provider Outage
- **Exact UI Click**: Click `[⚡ SIMULATE AN INCIDENT]` button (`#btn-launch-modal`).
- **Modal Opens**: Ensure `Billing Provider Outage` is selected (default).
- **Exact UI Click**: Click `[⚡ RUN AUTONOMOUS RECOVERY]` (`#btn-execute-scenario`).
- **Visual State Progression**:
  1. Incident Banner flashes: `INCIDENT DETECTED: BILLING PROVIDER UNAVAILABLE`.
  2. Node `01 DETECT` lights up: Ingests Stripe HTTP 500 error telemetry.
  3. Node `02 REASON` activates: Gemini correlates multi-provider health.
  4. `AUTONOMY DECISION` card confirms: `✓ POLICY ALLOWS AUTONOMOUS FAILOVER • Confidence: HIGH • Violations: 0`.
  5. Node `03 ACT` executes: `switch_payment_gateway(provider="adyen")` with unique OCC lease.
  6. Node `04 VERIFY` engages: State machine transitions to `VERIFYING`.
  7. Node `05 RECOVERED` turns emerald: Outcome contract satisfied.
- **Narration**:
  > "We trigger a primary payment provider outage. Stripe returns consecutive HTTP 500 timeouts.
  > 
  > Gemini correlates telemetry and determines that secondary gateway Adyen is healthy. The deterministic PolicyEngine checks blast radius and rate limits: autonomous action is permitted.
  > 
  > The tool executes. But notice: RecoveryOS does NOT mark the incident resolved yet."

---

### [1:30 – 1:50] 4. Recovery Proof & Independent Verification Probe
- **Visual Focus**: Zoom/scroll to the emerald **Evidence-Backed Recovery Proof** (`#recovery-proof-certificate`) and **Decision Inspector** (`#panel-inspector`).
- **Inspect Key Elements**:
  - `ACTION`: `switch_payment_gateway`
  - `VERIFICATION`: `Billing subscription probe → HTTP 200`
  - `INVARIANT PROOF`: `Agent Action Executed ≠ Recovery Proved`
  - `MTTR`: `~5.2s` | `OPERATOR INTERVENTIONS`: `0`
- **Narration**:
  > "Here is the core invariant. The agent executed `switch_payment_gateway`, but RecoveryOS dispatched an independent subscription verification probe directly to the gateway.
  > 
  > Only after the independent probe satisfied every required outcome did RecoveryOS render this evidence-backed Recovery Proof.
  > 
  > Over in the Decision Trace, we answer the four audit questions: What did you see, what did you think, what did you do, and how do you know it worked."

---

### [1:50 – 2:35] 5. Scenario 02: Contradictory Evidence (Bounded Autonomy)
- **Exact UI Click**: Click `[⚡ SIMULATE AN INCIDENT]`.
- **Modal Action**: Select radio `Contradictory Evidence`.
- **Exact UI Click**: Click `[⚡ TEST AUTONOMY BOUNDARY]`.
- **Visual State Progression**:
  1. Telemetry returns conflicting risk scores: Experian: 42 (Low Risk) vs. Equifax: 88 (High Risk).
  2. The system **halts**. Node `03 ACT` is NOT executed.
  3. Amber Alert Banner appears: `AUTONOMY BOUNDARY REACHED: HUMAN APPROVAL REQUIRED`.
  4. Decision card displays: `WHY WE STOPPED: Autonomy is governed, not assumed.`
- **Narration**:
  > "Now, what happens when autonomous action is unsafe?
  > 
  > In Scenario 2, verification providers return contradictory risk scores: Experian says 42, Equifax says 88.
  > 
  > An unconstrained agent might guess. RecoveryOS refuses to guess. Its deterministic policy engine detects the contradiction, halts execution at the autonomy boundary, and escalates to a human operator."
- **Exact UI Click**: Click `[✓ AUTHORIZE RECOVERY ACTION]` (`#btn-submit-approval`).
- **Narration**:
  > "The human operator reviews the conflicting audit card, signs off, and RecoveryOS safely completes the workflow."

---

### [2:35 – 3:10] 6. Scenario 03: Worker Interruption (Resilient OCC Recovery)
- **Exact UI Click**: Click `[⚡ SIMULATE AN INCIDENT]`.
- **Modal Action**: Select radio `Worker Interruption`.
- **Exact UI Click**: Click `[⚡ TEST RESILIENCE]`.
- **Visual State Progression**:
  1. A deterministic interruption occurs after the provider accepts a billing write but before local completion persists.
  2. OCC lease timeout expires (60s).
  3. Replacement worker claims lease, reconciles external state against ground truth, and resumes idempotently.
  4. Resilient badges upgrade to: `✓ NO DUPLICATE EXECUTION`, `✓ NO STATE CORRUPTION`, `✓ NO DOUBLE BILLING`.
- **Narration**:
  > "The recovery mechanism itself must survive infrastructure crashes.
  > 
  > In Scenario 3, the worker process is killed mid-mutation. Using Optimistic Concurrency Control (OCC) and 60-second leases stored in Firestore, a replacement worker reconciles external state and safely resumes without double-billing or duplicate execution."

---

### [3:10 – 3:35] 7. Google Cloud Architecture & Code Tour
- **Visual Focus**: Hero ribbon Google Cloud stack badges:
  `☁️ Cloud Run API • 📨 Cloud Pub/Sub Events • ⚡ Cloud Firestore OCC • 🤖 Gemini 3.5 Flash ADK`.
- **Code Pointers (Cut to README or IDE momentarily)**:
  - `backend/models/workflow.py`: `VALID_TRANSITIONS` forbids `EXECUTING → COMPLETED`.
  - `backend/engine/policy_engine.py`: Deterministic autonomy boundary.
  - `backend/engine/agent_runner.py`: Independent outcome verification gate.
- **Narration**:
  > "RecoveryOS is built natively on Google Cloud:
  > Cloud Run hosts our async FastAPI control plane, Cloud Pub/Sub handles distributed telemetry and worker dispatch, Cloud Firestore maintains distributed OCC leases and audit trails, and Google ADK orchestrates Gemini 3.5 Flash reasoning loops.
  > 
  > Every claim is backed by 377 automated regression tests and 30 targeted adversarial attack tests."

---

### [3:35 – 3:55] 8. Conclusion & Core Takeaway
- **Visual Focus**: Return to full Command Center with Recovery Proof visible.
- **Narration**:
  > "Autonomous operations cannot rely on blind agent confidence.
  > 
  > RecoveryOS provides the missing control plane: governed autonomy, resilient execution, and verified recovery proof.
  > 
  > Thank you."

---

## Critical Rehearsal Guidelines

### What NOT to Click
- Do **NOT** click the browser Back/Forward buttons during live SSE streaming.
- Do **NOT** rapidly spam the "SIMULATE AN INCIDENT" button while an active scenario is running.
- Do **NOT** open developer tools (F12) during the recording.

### What NOT to Say
- Do **NOT** use hyperbolic jargon ("world's first", "replaces all DevOps", "magical self-healing").
- Do **NOT** claim live banking integrations; state clearly that external SaaS providers use high-fidelity simulated services for reproducible testing.
