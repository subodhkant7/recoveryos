# Phase 33: Final Judge One-Take Script (2–3 Minutes)

This script is engineered for a live, uninterrupted hackathon presentation showing autonomous recovery, policy safety, outcome verification, and worker resilience.

---

## ⏱️ Live Presentation Timeline

### `0:00 - 0:20` — The Problem: Unverified Automation
- **Visual**: RecoveryOS Operator Command Center (`/console/`).
- **Speaker**:
  > "When enterprise infrastructure breaks, traditional automation runs a script and assumes it worked. If the script fails silently, engineers spend hours diagnosing what happened.
  >
  > We built **RecoveryOS** on a non-negotiable engineering principle: **Action Executed ≠ Recovery Verified**."

---

### `0:20 - 0:40` — The Core Thesis: Observe, Reason, Act, Verify, Prove
- **Visual**: Point to the 5-stage lifecycle graph at the top of the canvas:
  `01 DETECT` → `02 REASON` → `03 ACT` → `04 VERIFY` → `05 RECOVERED`.
- **Speaker**:
  > "RecoveryOS doesn't just execute playbooks. It actively **observes** failure signals, **reasons** about root causes, **acts** within policy boundaries, independently **verifies** outcomes, and generates an auditable **Recovery Proof**."

---

### `0:40 - 1:20` — Act 1: Autonomous Billing Outage Recovery
- **Action**:
  1. Click **`⚡ SIMULATE AN INCIDENT`**.
  2. Select **`SCENARIO 01 • Billing Provider Outage`**.
  3. Click **`⚡ RUN AUTONOMOUS RECOVERY`**.
- **Live Visual Walkthrough**:
  - Point to **`01 DETECT`**: The agent captures consecutive HTTP 500 signals from Stripe.
  - Point to **`02 REASON`**: Correlates failure, evaluates policy threshold (confidence: HIGH, violations: 0).
  - Point to **`03 ACT`**: Tool execution card fires `switch_payment_gateway(provider="adyen")`.
  - Point to **`04 VERIFY`**: The outcome probe verifies HTTP 200 on subscription creation.
  - Point to **`05 RECOVERED`**: The canvas illuminates green.
  - Point to the **Evidence-Backed Recovery Proof**:
    > "Notice the proof: MTTR is calculated from authoritative timestamps. The action result is separate from verification evidence. The outcome contract is fulfilled only after independent verification."

---

### `1:20 - 1:50` — Act 2: Safe Bounded Autonomy (Contradictory Evidence)
- **Action**:
  1. Click **`⚡ SIMULATE AN INCIDENT`**.
  2. Select **`SCENARIO 02 • Contradictory Evidence`**.
  3. Click **`⚠ TEST AUTONOMY BOUNDARY`**.
- **Live Visual Walkthrough**:
  - The workflow halts at `02 REASON`.
  - The **Autonomy Decision Card** shows `⚠ AUTONOMY BOUNDARY REACHED`.
  - The **Human Authorization Card** slides in.
- **Speaker**:
  > "True autonomy requires knowing when **not** to act.
  > Here, risk signals conflict across providers. Taking autonomous action risks compliance violation.
  > Instead of guessing, RecoveryOS safely halted and escalated to an authenticated human approver."
- **Action**:
  - Click **`✓ APPROVE RECOVERY`**.
  - Show the workflow transition to `OPERATOR AUTHORIZED`, resume, verify, and complete with `Operator Intervention: 1`.

---

### `1:50 - 2:20` — Act 3: Worker Interruption & Idempotent State Reconciliation
- **Action**:
  1. Click **`⚡ SIMULATE AN INCIDENT`**.
  2. Select **`SCENARIO 03 • Worker Interruption`**.
  3. Click **`⚡ TEST RESILIENCE`**.
- **Live Visual Walkthrough**:
  - The demo simulates an interruption after the billing provider accepts a write but before local completion persists.
  - The OCC lease expires.
  - The replacement worker reconciles external state against payment records and resumes idempotently.
  - The **Worker Resilience Card** upgrades its evidence badges:
    - `✓ NO DUPLICATE EXECUTION`
    - `✓ NO STATE CORRUPTION`
    - `✓ NO DOUBLE BILLING`

---

### `2:20 - 2:40` — Conclusion & Central Value
- **Speaker**:
  > "Traditional automation proves that a command executed.
  > **RecoveryOS proves that the system recovered.**
  >
  > Autonomous background execution, safe bounded escalation, persistent state, and outcome verification. Thank you."
