# RecoveryOS — System Architecture & Control Plane Contract

> **Core Invariant**: `Action Executed ≠ Recovery Verified`
> **Governance Invariant**: `Autonomy is governed, not assumed.`

RecoveryOS is a **multi-agent enterprise recovery control plane with explicit separation of concerns**. It provides a governed reliability layer around autonomous enterprise workflows, ensuring that failure diagnosis, state mutation, policy enforcement, and outcome verification remain strictly distinct.

---

## 1. Multi-Agent Architecture Classification

**Architecture Class: Class C (Specialist Routing & Planning with Single Execution Agent)**

```
                  ENTERPRISE AGENT FLEET
                           │
                           ▼
                    ORCHESTRATOR
                           │
                ┌──────────┴──────────┐
                │                     │
                ▼                     ▼
       TASKMASTER EXECUTION    RECOVERY SPECIALIST
        (Mutating Agent)       (Read-Only Diagnosis)
                │                     │
                │                     ▼
                │              RECOVERY PLAN
                │                     │
                └──────────┬──────────┘
                           │
                           ▼
                    AGENT GATEWAY
                           │
                    ┌──────┴──────┐
                    │             │
                 IDENTITY     GUARDRAILS
                    │             │
                    └──────┬──────┘
                           │
                           ▼
                      POLICY ENGINE
                           │
                           ▼
                    MUTATING TOOLS
                           │
                           ▼
               INDEPENDENT VERIFICATION
                           │
                           ▼
                    FIRESTORE EVIDENCE
                           │
                    ┌──────┴──────┐
                    │             │
                    ▼             ▼
                  PROVE       RECOVER /
                              ESCALATE
```

---

## 2. Agent Responsibilities & Separation of Concerns

RecoveryOS enforces strict separation between **reasoning/diagnosis**, **state mutation**, **policy authorization**, and **ground-truth verification**:

### 1. Orchestrator (`recoveryos_orchestrator`)
- **Type**: Google ADK `LlmAgent`
- **Responsibility**: Top-level conversation coordinator. Delegates business process steps to the execution agent (`taskmaster`) and failure diagnosis to the diagnostic agent (`recovery_specialist`).
- **Boundaries**: Manages conversation flow and sub-agent handoffs; does not execute tools directly.

### 2. Recovery Specialist (`recovery_specialist`)
- **Type**: Dedicated Read-Only Google ADK `LlmAgent` (Gemini 3.5 Flash)
- **Responsibility**: Diagnostic and recovery planning agent. Called when a step fails or an outcome is blocked.
- **Why Read-Only?**: Diagnosis and recovery planning are deliberately decoupled from state mutation. The system must never confuse generating a recovery plan with executing an action.
- **Tools**: Read-only diagnostic tools (`check_service_status`, `list_available_billing_providers`, `get_workflow_state`, `submit_recovery_plan`). Cannot call mutating tools.

### 3. Taskmaster (`taskmaster`)
- **Type**: Unified Mutating Execution Google ADK `LlmAgent`
- **Responsibility**: Primary execution agent for normal onboarding steps and approved recovery actions.
- **Why a Single Execution Boundary?**: A single execution agent simplifies policy enforcement, distributed operation claiming, canonical idempotency key generation, optimistic concurrency control (OCC), and audit trails.
- **Tools**: Mutating tools (`verify_identity`, `validate_documents`, `run_risk_check`, `setup_billing`, `activate_account`, `send_welcome_package`) and verification tools.

### 4. Verification Agent (`verification-agent`)
- **Type**: Deterministic Ground-Truth Verifier
- **Responsibility**: Independent query probe against external services to confirm business outcomes match the `OutcomeContract`.
- **Invariance**: An action response (e.g. `HTTP 200` from a tool) is never accepted as verification. Only an independent probe update to the contract state counts as verified.

### 5. Fleet Specialists (`billing-agent`, `risk-agent`, `identity-agent`)
- **Type**: Typed Agent Cards & Zero-Trust Scope Boundaries
- **Responsibility**: Explicit capability, tenant, and data scope declarations in the Agent Registry. Enforced by the Agent Gateway to establish least-privilege boundaries around shared tool execution.

---

## 3. The End-to-End Execution Pipeline

Every tool call in the RecoveryOS agent pipeline follows an immutable sequence:

```
Agent Request (e.g. setup_billing)
      │
      ▼
1. Agent Identity Resolution (Registry lookup, tenant scope, capability match)
      │
      ▼
2. RecoveryOS Agent Guardrails (Deterministic scan for credentials, injection, unknown tools)
      │
      ▼
3. Agent Gateway (Evaluates identity + scopes; logs auditable GatewayDecision)
      │
      ▼
4. Deterministic PolicyEngine (Pure Python rules: ordering, OCC, idempotency, budget)
      │
      ▼
5. Distributed Operation Claim (Transaction lease, canonical idempotency key)
      │
      ▼
6. Authoritative External Reconciliation (Crash recovery check before mutation)
      │
      ▼
7. External Service Mutation (Simulated or live enterprise endpoint)
      │
      ▼
8. Unique, Durable Evidence Records & Durable Context (Firestore persistence, context store update)
      │
      ▼
9. Independent Verification Query (Separate query to verify actual state)
      │
      ▼
10. Recovery Proof / State Transition (RECOVERED • VERIFIED or AWAITING_APPROVAL)
```

---

## 4. First-Party Equivalents Statement

RecoveryOS implements first-party equivalents for enterprise agent fleet capabilities rather than claiming integration with Gemini Enterprise Agent Platform (GEAP) products:

| Fleet Requirement | RecoveryOS First-Party Implementation |
|---|---|
| Agent Discovery & Cards | `AgentRegistry` (`backend/fleet/registry.py`) |
| Zero-Trust Agent Scopes | `AgentIdentity` (`backend/fleet/identity.py`) |
| Central Policy Boundary | `AgentGateway` (`backend/fleet/gateway.py`) |
| Durable Agent State | `AgentContextStore` (`backend/fleet/context_store.py`) |
| Safety & Injection Checks | `AgentGuardrails` (`backend/fleet/guardrails.py`) |
| OpenTelemetry-Compatible Traces | `FleetTracer` (`backend/fleet/observability.py`) |
| Failure-Tolerant Routing | `AgentRouter` (`backend/fleet/routing.py`) |

---

## 5. Infrastructure & Runtime Architecture

- **Reasoning**: Gemini 3.5 Flash via Google ADK (`google-adk`).
- **Compute**: Cloud Run hosting FastAPI API server and asynchronous worker consumers.
- **Persistence**: Cloud Firestore (`FirestoreWorkflowStore`) with optimistic concurrency control (OCC), distributed leases, and append-only event logs.
- **Messaging**: Cloud Pub/Sub (`GooglePubSubPublisher`) for asynchronous workflow dispatch and worker decoupling.
