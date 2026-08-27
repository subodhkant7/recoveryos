# RecoveryOS — Phase 38 Judge Demonstration Guide

> **A recovery-first control plane for autonomous operations.**

This guide provides the exact timed 4-minute demonstration script for hackathon judges, highlighting the central thesis: **Action Executed ≠ Recovery Verified**.

---

## Central Judge Takeaway

> **"RecoveryOS is not another agent that performs an action; it is a control plane that governs autonomous action and refuses to call recovery successful until the outcome is independently verified."**

---

## 4-Minute Timed Demonstration Sequence

### [0:00 - 0:30] — The Problem & Central Thesis (The "Why")
- **Screen**: Operator Command Center (`http://localhost:8000/console/`).
- **Narrative**:
  > "Traditional automation executes playbooks: `DETECT → RUN PLAYBOOK`. If an automated script finishes with exit code 0, traditional systems assume success. But in production, executing an action does NOT mean the system recovered. Downstream databases may be corrupted, billing may fail silently, or failovers may double-bill customers.
  > 
  > **Autonomous operations need a recovery control plane.**
  > 
  > RecoveryOS enforces a strict invariant: **Action Executed ≠ Recovery Verified**. It allows agents to act only within explicit policy boundaries, independently verifies required outcomes, and issues cryptographic Recovery Proof certificates."

---

### [0:30 - 1:45] — Scenario 01: Autonomous Recovery (The "Hero Flow")
- **Action**: Click `⚡ SIMULATE AN INCIDENT` → Choose **Billing Provider Outage** → Click `⚡ RUN AUTONOMOUS RECOVERY`.
- **Visual Progression**:
  1. **`01 DETECT`**: Stripe API experiences consecutive HTTP 500 timeouts. Signal is ingested via Cloud Pub/Sub.
  2. **`02 REASON`**: Gemini / ADK agent correlates failure signatures and evaluates policy. The `AUTONOMY DECISION` card shows: `✓ POLICY ALLOWS AUTONOMOUS FAILOVER • Confidence: HIGH • Constraint Violations: 0`.
  3. **`03 ACT`**: Tool `switch_payment_gateway(provider="adyen")` executes with a unique OCC idempotency key.
  4. **`04 VERIFY`**: The state machine moves to `VERIFYING`. An active independent subscription probe queries the Adyen gateway.
  5. **`05 RECOVERED`**: All contract outcomes pass (`contract.all_verified() == True`).
- **Inspect**:
  - Show the **"Why Did You Do That?"** decision trace (Questions 01–04).
  - Highlight the **Recovery Proof Certificate**:
    - `INCIDENT`: Billing Provider Unavailable
    - `ACTION`: switch_payment_gateway
    - `VERIFICATION`: Billing subscription probe → HTTP 200
    - `INVARIANT PROOF`: *Agent Action Executed ≠ Recovery Proved. Verified via Independent Subscription Probe.*

---

### [1:45 - 2:45] — Scenario 02: Bounded Autonomy (The "Refusal to Guess")
- **Action**: Click `⚡ SIMULATE AN INCIDENT` → Choose **Contradictory Evidence** → Click `⚡ TEST AUTONOMY BOUNDARY`.
- **Visual Progression**:
  1. Multi-provider credit checks return conflicting risk scores: Experian: 42 (Approved) vs. Equifax: 88 (Flagged).
  2. **Autonomous Execution Safely Halts**: The system refuses to guess.
  3. The canvas displays: **`AUTONOMY BOUNDARY REACHED: HUMAN APPROVAL REQUIRED`**.
  4. Decision card updates to `⚠ AUTONOMY BOUNDARY REACHED`.
- **Narrative**:
  > "Here, RecoveryOS proves that autonomy is governed, not assumed. Because evidence is contradictory, autonomous failover would violate compliance. The system stops and escalates to a human operator."
- **Action**: Click `✓ AUTHORIZE RECOVERY ACTION`. Show operator sign-off and safe resumption.

---

### [2:45 - 3:30] — Scenario 03: Worker Resilience & Replay (The "Execution Safety")
- **Action**: Click `⚡ SIMULATE AN INCIDENT` → Choose **Worker Interruption** → Click `⚡ TEST RESILIENCE`.
- **Visual Progression**:
  1. Worker process crashes mid-operation.
  2. The OCC operation lease expires (60s).
  3. Replacement worker reconciles state against external ground truth before resuming.
  4. Evidence-gated badges upgrade to: `✓ NO DUPLICATE EXECUTION`, `✓ NO STATE CORRUPTION`, `✓ NO DOUBLE BILLING`.
- **Narrative**:
  > "The recovery mechanism itself must survive crashes. OCC leases and step idempotency guarantee zero duplicate billing."
- **Action**: Click `↺ DECISION REPLAY` → Click `▶ PLAY` and `⏭ STEP`. Show deterministic, read-only reconstruction of historical decision events.

---

### [3:30 - 4:00] — Summary & Technical Proof (The "Credibility Close")
- **Point out the Architecture**:
  - Cloud Run (Control Plane API & Console).
  - Cloud Pub/Sub (Event streaming & telemetry).
  - Cloud Firestore (OCC leases & idempotency store).
  - Gemini 1.5 Pro & Google ADK (Agent reasoning loop).
- **Close**:
  > "RecoveryOS governs autonomous operations with guardrails, independent verification, and proof."
