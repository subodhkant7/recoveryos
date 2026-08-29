# RecoveryOS — Phase 39 Browser Demo Automation & DOM Selector Map

This document catalogs the stable DOM selectors and automated interaction steps for orchestrating the RecoveryOS demonstration.

---

## 1. Stable DOM Selector Reference Map

| Component | DOM Selector / ID | Purpose & Trigger |
|:---|:---|:---|
| **Simulate Incident Button** | `#btn-launch-modal` | Opens the Scenario Launcher Modal |
| **Modal Overlay** | `#modal-scenario-launcher` | Container for scenario selection |
| **Billing Outage Radio** | `input[value="billing_unavailable"]` | Selects Scenario 01 (Hero Flow) |
| **Contradictory Radio** | `input[value="contradictory_evidence"]` | Selects Scenario 02 (Autonomy Boundary) |
| **Worker Interruption Radio** | `input[value="worker_interruption"]` | Selects Scenario 03 (OCC Resilience) |
| **Modal Action CTA** | `#btn-execute-scenario` | Dispatches scenario to `/api/scenarios/{name}` |
| **Node 01: Detect** | `#node-detect` | Highlights signal ingestion stage |
| **Node 02: Reason** | `#node-reason` | Highlights Gemini reasoning stage |
| **Node 03: Act** | `#node-recover` | Highlights tool execution stage |
| **Node 04: Verify** | `#node-verify` | Highlights outcome verification stage |
| **Node 05: Recovered** | `#node-recovered` | Highlights completed recovery stage |
| **Autonomy Decision Card** | `#autonomy-decision-card` | Shows policy evaluation status |
| **Approval Banner** | `#approval-action-card` | Displays Human-in-the-Loop escalation |
| **Authorize Button** | `#btn-submit-approval` | Submits operator approval |
| **Worker Resilience Card**| `#worker-resilience-card` | Displays OCC lease reconciliation badges |
| **Evidence-Backed Recovery Proof** | `#recovery-proof-certificate` | Final proof card showing independent verification evidence |
| **Decision Trace Inspector** | `#panel-inspector` | Explains 4 core decision audit questions |
| **Demo Mode Toggle** | `#btn-toggle-demo-mode` | Expands canvas to presentation width |
| **Replay Play Button** | `#btn-replay-play` | Triggers read-only deterministic replay |

---

## 2. Programmatic Interaction Sequence

```javascript
// Step 1: Launch Billing Provider Outage (Hero Scenario)
document.getElementById('btn-launch-modal').click();
document.querySelector('input[value="billing_unavailable"]').click();
document.getElementById('btn-execute-scenario').click();

// Step 2: Highlight Inspector Verification Question (Question 04)
document.getElementById('audit-know').scrollIntoView({ behavior: 'smooth' });

// Step 3: Launch Contradictory Evidence Scenario
document.getElementById('btn-launch-modal').click();
document.querySelector('input[value="contradictory_evidence"]').click();
document.getElementById('btn-execute-scenario').click();

// Step 4: Authorize Escalated Action
setTimeout(() => {
  document.getElementById('btn-submit-approval')?.click();
}, 2000);

// Step 5: Launch Worker Interruption Scenario
document.getElementById('btn-launch-modal').click();
document.querySelector('input[value="worker_interruption"]').click();
document.getElementById('btn-execute-scenario').click();
```
