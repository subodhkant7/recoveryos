# Phase 40 — Founder Voiceover Timing & Narration Script

**Video Track:** `artifacts/recoveryos_judge_demo_silent.mp4`  
**Total Target Duration:** **03:48.00** (228 seconds)  
**Target Speaking Pace:** 130–140 words per minute (Calm, authoritative, engineering-first tone)

---

## Word-for-Word Narration Script with Timestamp Cues

### Segment 1: Command Center Overview & Core Thesis (`00:00 – 00:18` • 18s)
* **Visual on Screen:** Command Center HUD, Google Cloud Stack ribbon, Core Invariant Banner.
* **Founder Voiceover:**
  > *"This is RecoveryOS — the recovery-first control plane for autonomous operations.*  
  > *In production systems, traditional automation assumes success the moment a script exits with code zero.*  
  > *Our core invariant is simple: **Action Executed is NOT Recovery Verified**.*  
  > *RecoveryOS treats verification as part of recovery itself."*

---

### Segment 2: 5-Stage Agentic Control Loop (`00:18 – 00:32` • 14s)
* **Visual on Screen:** 5-Stage agentic lifecycle cards (`DETECT` → `REASON` → `ACT` → `VERIFY` → `RECOVERED`).
* **Founder Voiceover:**
  > *"Every recovery follows a deterministic 5-stage control loop:*  
  > *Detect signals via Pub/Sub, reason about root causes with Gemini 3.5 Flash, execute bounded idempotent tools with OCC leases, independently verify outcomes against ground truth, and render an evidence-backed Recovery Proof."*

---

### Segment 3: Hero Scenario 01 — Billing Provider Outage (`00:32 – 01:24` • 52s)
* **Visual on Screen:** Stripe HTTP 500 incident banner, Gemini reasoning loop, Adyen gateway failover execution, active verification probe.
* **Founder Voiceover:**
  > *"Let's see Scenario 1 in action: a live billing provider outage.*  
  > *Our primary payment gateway, Stripe, begins failing with consecutive HTTP 500 timeouts.*  
  > *The Taskmaster agent ingests the error stream and invokes Gemini 3.5 Flash.*
  > *Gemini diagnoses the degraded gateway and verifies that the secondary provider, Adyen, is healthy.*  
  > *Before executing, our deterministic Policy Engine evaluates the action. Policy confirms confidence is high, zero constraints are violated, and autonomous failover is permitted.*  
  > *The recovery agent claims an OCC lease and executes the gateway switch tool.*  
  > *Notice that the state machine transitions to `VERIFYING`, refusing to declare recovery until independent active subscription probes confirm transaction processing on Adyen."*

---

### Segment 4: Recovery Proof & Independent Verification (`01:24 – 01:42` • 18s)
* **Visual on Screen:** Emerald Evidence-Backed Recovery Proof, intervention count, authoritative MTTR, verification evidence IDs, Invariant Proof Bar, 4-Questions Decision Trace.
* **Founder Voiceover:**
  > *"Once independent verification satisfies every required outcome, RecoveryOS renders the Evidence-Backed Recovery Proof.*
  > *Mean Time to Recover was 5.2 seconds with zero human interventions.*  
  > *Judges can inspect the 4 core audit questions in the Decision Trace: What did you see? What did you think? What did you do? And how do you know it worked?"*

---

### Segment 5: Scenario 02 — Contradictory Evidence & Autonomy Boundary (`01:42 – 02:24` • 42s)
* **Visual on Screen:** Amber Autonomy Boundary alert, Experian (42) vs Equifax (88) conflict, human authorization gate button.
* **Founder Voiceover:**
  > *"What happens when telemetry is ambiguous? In Scenario 2, two credit risk providers return contradictory evidence: Experian reports a safe risk score of 42, while Equifax flags a severe score of 88.*  
  > *Unbounded AI agents would hallucinate or guess. RecoveryOS enforces strict autonomy boundaries.*  
  > *The system halts with `AUTONOMY BOUNDARY REACHED: HUMAN APPROVAL REQUIRED`.*  
  > *Autonomy is governed, not assumed. The agent presents its diagnosis, and only an authorized human operator can approve the recovery action."*

---

### Segment 6: Scenario 03 — Worker Interruption & OCC Resilience (`02:24 – 02:57` • 33s)
* **Visual on Screen:** Post-write/pre-persistence interruption alert, authoritative-state reconciliation badges (`NO DUPLICATE EXECUTION`, `NO DOUBLE BILLING`), Decision Replay Engine.
* **Founder Voiceover:**
  > *"Recovery systems must survive failure themselves. In Scenario 3, a worker process is terminated mid-flight.*  
  > *Using Cloud Firestore Optimistic Concurrency Control, the 60-second execution lease expires.*  
  > *A replacement worker acquires the lease, reconciles external state against ground truth, and safely completes the workflow with zero duplicate charges, zero state corruption, and zero double-billing.*  
  > *Our built-in Replay Engine allows operators to step through any historical incident deterministically without mutating network state."*

---

### Segment 7: Google Cloud Architecture & Code Tour (`02:57 – 03:21` • 24s)
* **Visual on Screen:** Architecture Blueprint (Cloud Run, Pub/Sub, Firestore OCC, Gemini 3.5 Flash ADK) and verified codebase invariants.
* **Founder Voiceover:**
  > *"RecoveryOS is built natively on Google Cloud:*  
  > *Cloud Run powers our async control plane and single-use SSE ticket stream.*  
  > *Cloud Pub/Sub handles decoupled event distribution.*  
  > *Cloud Firestore provides distributed OCC leases and idempotency deduplication.*  
  > *And Gemini 3.5 Flash with the Google Agent Development Kit provides bounded reasoning.*
  > *Our entire test suite of 377 automated tests and 30 targeted judge attack tests validates every invariant in production."*

---

### Segment 8: Final Thesis & Conclusion (`03:21 – 03:48` • 27s)
* **Visual on Screen:** Final takeaway banner: *Govern. Recover. Verify. Prove.*
* **Founder Voiceover:**
  > *"Autonomous operations don't just need agents — they need a recovery control plane.*  
  > *RecoveryOS gives autonomous systems explicit autonomy boundaries, durable resilience, and independent outcome verification.*  
  > *RecoveryOS: Govern. Recover. Verify. Prove.*  
  > *Thank you."*

---

## Summary Timing Quick-Sheet

| Timestamp | Word Count | Speaking Rate | Key Visual Event |
|---|:---:|:---:|---|
| `00:00 – 00:18` | 46 words | 153 wpm | Control Center HUD & Hero Ribbon |
| `00:18 – 00:32` | 38 words | 162 wpm | 5-Stage Agent Orchestration Graph |
| `00:32 – 01:24` | 125 words | 144 wpm | Billing Outage Autonomous Failover |
| `01:24 – 01:42` | 49 words | 163 wpm | Recovery Proof, verification evidence & MTTR |
| `01:42 – 02:24` | 94 words | 134 wpm | Bounded Autonomy & Human Approval |
| `02:24 – 02:57` | 82 words | 149 wpm | OCC Lease Resilience & Replay Engine |
| `02:57 – 03:21` | 68 words | 170 wpm | Google Cloud Architecture Blueprint |
| `03:21 – 03:48` | 44 words | 97 wpm | Final Thesis & Stable Exit |
| **Total** | **546 words** | **143 wpm** | **Exact Duration: 03:48.00** |
