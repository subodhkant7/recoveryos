# Hackathon Submission: RecoveryOS

## Title

**RecoveryOS — Governed Autonomous Recovery for Enterprise Agent Fleets**

## One-line pitch

> The reliability layer that verifies whether autonomous agents actually recovered the business outcome.

## Problem

Enterprise agents can execute a tool successfully while the business outcome is still broken. An `HTTP 200` does not prove a subscription is active on the correct plan, that a record is consistent, or that a crash did not duplicate a side effect.

## Solution

RecoveryOS detects failures, coordinates a recovery workflow, applies deterministic policy gates, executes bounded idempotent actions, independently verifies required outcomes, records evidence, and escalates when autonomy is insufficient.

Its invariant is simple: **Action Executed ≠ Recovery Verified.**

## Differentiator

Gemini may diagnose and propose; deterministic RecoveryOS code enforces policy, state transitions, idempotency, and the independent verification gate. A workflow becomes `RECOVERED • VERIFIED` only when every required outcome in its contract has separate verification evidence.

The console renders an **Evidence-Backed Recovery Proof** from durable workflow data: workflow ID, incident, action and result, verification method, evidence IDs, outcome count, UTC completion time, final lifecycle, and intervention count. It is not cryptographically signed or presented as tamper-evident.

## Fortified Enterprise Fleet fit

RecoveryOS provides the control and reliability layer an enterprise fleet needs around agent actions:

- policy-gated autonomy and human approval boundaries;
- bounded recovery retries, liveness protection, and escalation;
- idempotency, operation claims, and optimistic concurrency protection;
- independent verification that blocks false recovery;
- auditable event history and read-only historical replay.

## Google technologies used

- **Gemini 3.5 Flash** (`GEMINI_MODEL`) for recovery reasoning.
- **Google ADK** for agents, delegated tools, sessions, and runner execution.
- **Cloud Run** for the API and worker deployment runtime.
- **Cloud Firestore** for durable workflow state, evidence, audit history, idempotency records, and operation claims.
- **Cloud Pub/Sub** for asynchronous workflow dispatch and worker consumption.

RecoveryOS does not claim Gemini Enterprise Agent Platform (GEAP) services. Its governance, policy, state machine, verification, evidence proof, retries, and replay are RecoveryOS-native.

## Demo flow

1. **Billing provider outage** — A simulated primary provider returns `HTTP 503`; an ADK/Gemini agent diagnoses, policy permits an idempotent failover, and an independent billing query verifies the required plan and billing cycle before `RECOVERED • VERIFIED`.
2. **Contradictory evidence** — The billing action reports success but returns the wrong plan tier. Independent verification fails, so RecoveryOS cannot declare recovery and stops at `AWAITING APPROVAL`.
3. **Worker interruption** — A deterministic post-write/pre-persistence interruption is reconciled against external state, then redispatched within the recovery budget without duplicate billing.

## Evaluation path

Open the [production command center](https://recoveryos-321161003794.asia-east1.run.app/), run **Billing provider outage**, then inspect the lifecycle, policy decision, action result, verification evidence, and Evidence-Backed Recovery Proof. Run **Contradictory evidence** next to see the safety boundary rather than a false success.

## Code evidence

- Runtime model and ADK agent construction: [`backend/config.py`](../backend/config.py), [`backend/agents/agent_factory.py`](../backend/agents/agent_factory.py)
- Enterprise Agent Fleet Control Plane (Registry, Identity, Gateway, Context, Guardrails, Observability, Routing): [`backend/fleet/`](../backend/fleet/)
- Architecture specification: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)
- Independent verification completion gate: [`backend/engine/agent_runner.py`](../backend/engine/agent_runner.py), [`backend/tools/onboarding/tools.py`](../backend/tools/onboarding/tools.py)
- Policy enforcement: [`backend/engine/policy_engine.py`](../backend/engine/policy_engine.py)
- Durable state and claims: [`backend/persistence/workflow_store.py`](../backend/persistence/workflow_store.py)
- Pub/Sub worker dispatch: [`backend/events/publisher.py`](../backend/events/publisher.py), [`backend/events/consumer.py`](../backend/events/consumer.py)
