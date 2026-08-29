# Phase 40 — Automated Silent Judge Demo Video Specification

**Status:** COMPLETE & CERTIFIED  
**Output Artifact:** `artifacts/recoveryos_judge_demo_silent.mp4`  
**Video Target Duration:** 3:45–3:55 (Actual: **03:48.00 / 228.0 seconds**)  
**Video Resolution:** **1920×1080 (16:9)** @ 30.0 FPS  
**Format:** H.264 / MP4 (yuv420p, progressive)  
**File Size:** 6.03 MB  
**Audio Track:** Silent (Prepared for separate founder voiceover track)

---

## 1. Executive Summary & Core Positioning

RecoveryOS is **a recovery-first control plane for autonomous operations**.

The video demonstrates the core operational reality:
> **Agent Action Executed ≠ Recovery Verified.**  
> Executing a recovery action or playbook is not proof that the system recovered. RecoveryOS governs autonomous operations with explicit autonomy boundaries and never declares recovery successful until the outcome has been independently verified against ground truth.

---

## 2. Automated Video Pipeline Architecture

The entire video recording and compilation process is **100% automated, deterministic, and reproducible** via a single script execution:

```bash
./scripts/record_judge_demo.sh
```

### Pipeline Components

1. **Pipeline Runner (`scripts/record_judge_demo.sh`)**:
   - Validates Python virtualenv and system `ffmpeg` binary.
   - Executes the deterministic rendering engine.
   - Verifies the integrity and byte size of the generated `.mp4`.

2. **Deterministic Rendering Engine (`scripts/record_judge_demo.py`)**:
   - Generates pixel-accurate 1080p frames representing the real RecoveryOS Command Center UI.
   - Applies live HUD ribbons, Google Cloud badges, 5-stage orchestration state graphs, scenario decision traces, and evidence-backed recovery proofs.
   - Encodes exact-duration H.264 video segments at 30 FPS.
   - Concatenates the segments seamlessly into `artifacts/recoveryos_judge_demo_silent.mp4`.

---

## 3. Video Structure & Segment Breakdown (3m 48s Total)

| Segment | Timestamp | Duration | UI / Narrative Focus |
|---|---|---|---|
| **01** | `0:00 – 0:18` | 18s | **Command Center Overview & Core Thesis:** Hero ribbon HUD, live Google Cloud stack (`Cloud Run`, `Pub/Sub`, `Firestore OCC`, `Gemini ADK`), and core invariant: *Action Executed ≠ Recovery Verified*. |
| **02** | `0:18 – 0:32` | 14s | **5-Stage Control Loop Flow:** Visual walk of `01 DETECT` → `02 REASON` → `03 ACT` → `04 VERIFY` → `05 RECOVERED`. |
| **03** | `0:32 – 1:24` | 52s | **Hero Scenario 01 (Billing Provider Outage):** Stripe HTTP 500 alert, Gemini reasoning, policy check (`PASS`), autonomous tool execution (`switch_payment_gateway`), and engagement of the verification gate. |
| **04** | `1:24 – 1:42` | 18s | **Evidence-Backed Recovery Proof & Independent Probe:** Verified recovery badge, intervention count, authoritative MTTR, verification evidence IDs, and Invariant Proof bar. |
| **05** | `1:42 – 2:24` | 42s | **Scenario 02 (Contradictory Evidence & Autonomy Boundary):** Experian (42) vs Equifax (88) conflict, `AUTONOMY BOUNDARY REACHED: HUMAN APPROVAL REQUIRED`, "Why we stopped" card, and operator sign-off button. |
| **06** | `2:24 – 2:57` | 33s | **Scenario 03 (Worker Interruption & OCC Resilience):** Deterministic post-write/pre-persistence interruption, state reconciliation against ground truth, evidence-gated resilience badges, and Decision Replay Engine. |
| **07** | `2:57 – 3:21` | 24s | **Google Cloud Architecture & Code Tour:** Architecture blueprint highlighting `Cloud Run` (`backend/api/server.py`), `Cloud Pub/Sub` (`backend/events/publisher.py`), `Firestore` (`backend/persistence/workflow_store.py`), and `Gemini ADK` (`backend/agents/agent_factory.py`). |
| **08** | `3:21 – 3:48` | 27s | **Final Thesis & Stable Hold Frame:** Final core takeaway: *Govern. Recover. Verify. Prove.* |

---

## 4. Verification & Audit Results

- **Executable File:** `scripts/record_judge_demo.sh` (Mode: `0755`)
- **Video Path:** `artifacts/recoveryos_judge_demo_silent.mp4`
- **Duration Check:** `228.00s` (Within 3:45–3:55 range; well below 3:59 hard limit)
- **Resolution Check:** `1920x1080` (1080p Full HD)
- **Container Format:** ISO Media MP4 (H.264 / AVC)
- **No Sensitive Secrets Exposed:** Clean environment variables, zero API keys embedded.
