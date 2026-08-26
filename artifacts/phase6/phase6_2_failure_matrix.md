# Phase 6.2: Distributed Failure & Edge Case Matrix

---

## 1. Matrix Overview
This matrix maps every distributed failure scenario in the asynchronous Pub/Sub $\rightarrow$ Worker $\rightarrow$ Gemini $\rightarrow$ Firestore pipeline to its mitigation, recovery behavior, test strategy, and invariant verification.

---

## 2. Distributed Failure Matrix

| Failure ID | Failure Mode / Scenario | Root Cause | System Defense & Mitigation | Recovery Behavior | Verification Test | Invariant Maintained |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **FAIL-01** | Pub/Sub Duplicate Delivery | Network timeout between worker and Pub/Sub | `OperationClaim` lease + message deduplication ID in Firestore | Second worker detects active lease/completion; drops message with immediate ACK | `test_pubsub_duplicate_delivery` | Effectively-once mutation |
| **FAIL-02** | Worker Crash Before Tool Execution | Container OOM / host preemption | Message unacknowledged; ack deadline expires (60s) | Pub/Sub redelivers to another worker; clean execution starts | `test_worker_crash_before_tool` | Zero workflow stranding |
| **FAIL-03** | Worker Crash Mid-External Mutation | Process killed while provider API is processing | Canonical idempotency key submitted to external provider | Reconciler queries provider state; commits completion without second charge | `test_worker_crash_mid_mutation` | Effectively-once mutation |
| **FAIL-04** | Worker Crash After Mutation Before Firestore Commit | Network drop after provider 200 OK | Idempotency record missing in store, but provider holds state | Startup reconciler polls provider, recovers state, writes completion | `test_worker_crash_post_mutation` | No state divergence |
| **FAIL-05** | Concurrent Worker Race on Same Workflow | Worker A and Worker B consume parallel messages | Firestore OCC `@firestore.async_transactional` | Stale worker raises `StaleWorkflowStateError`; aborts without corrupting state | `test_concurrent_worker_race` | OCC integrity |
| **FAIL-06** | Gemini Rate Limit (429) Burst | Rapid succession of LLM calls across workers | Cloud Tasks dispatch rate + Firestore leased token window | Cloud Tasks queues task; backoff with jitter retries before quota breach | `test_distributed_gemini_pacing` | $\le 15\text{ RPM}$ compliance |
| **FAIL-07** | Gemini Full Outage (503 / Circuit Open) | External Google AI platform outage | `GeminiCircuitBreaker` trips after 5 consecutive failures | Fast-fails in-flight turns; transitions workflow to `UNKNOWN` with audit event | `test_gemini_circuit_tripped` | Controlled failure |
| **FAIL-08** | Poison Message / Malformed Payload | Unparseable JSON or schema mismatch in topic | Schema validation middleware on worker ingress | Message discarded, sent to dead-letter topic; target workflow marked `ESCALATED` | `test_poison_message_deadletter` | Queue unblocking |
| **FAIL-09** | Repeated Worker Exceptions (5 Retries) | Unrecoverable bug in agent turn | Pub/Sub `max_delivery_attempts: 5` | Forwarded to `recoveryos-workflow-deadletter`; Cloud Monitoring alert fired | `test_deadletter_routing` | Dead-letter isolation |
| **FAIL-10** | Message Targeting Terminal Workflow | Delayed message for `COMPLETED` workflow | `WorkflowEngine` transition guard (`VALID_TRANSITIONS`) | Transition rejected; message acknowledged and dropped | `test_terminal_message_rejected` | Terminal immutability |
| **FAIL-11** | Cross-Tenant Forged Message | Attacker publishes message claiming Tenant A | Worker verifies cryptographic trace context & tenant filter | Tool execution rejected (`HTTP 403 / SecurityDenied`); audit logged | `test_tenant_forged_message` | Tenant isolation |
| **FAIL-12** | Worker SIGTERM During Agent Execution | Cloud Run instance scaled down or revision rotated | `ShutdownManager` drains active agent tasks within 5 seconds | In-flight step completes; if exceeded, transaction rolls back cleanly | `test_worker_sigterm_draining` | Graceful shutdown |
| **FAIL-13** | Firestore Transaction Contention | Hot document concurrent writes | Exponential OCC retry (up to 3 attempts) | Transaction automatically retries with updated snapshot | `test_firestore_occ_contention` | Transaction consistency |
| **FAIL-14** | Human Approval Timeout / Rejection | Operator rejects or ignores recovery plan | Status updated to `ESCALATED` or remains `AWAITING_APPROVAL` | Agent stops execution; external mutations blocked | `test_approval_rejection_safety` | Policy superiority |
| **FAIL-15** | Prompt Injection via Pub/Sub Payload | Malicious instruction embedded in scenario data | Sovereign `OutcomeContract` & deterministic Python policy engine | LLM hallucinated verification rejected; outcome requires real tool evidence | `test_prompt_injection_safety` | Verification sovereignty |

---

## 3. Key Invariant Guarantees
1. **At-Least-Once Delivery $\rightarrow$ Exactly-Once Effects:** Guaranteed via Firestore `OperationClaim` leases and provider idempotency keys.
2. **Deterministic Sovereignty:** Deterministic code always enforces state transitions; LLM outputs remain purely advisory.
3. **Fail-Closed Quota Safety:** Cloud Tasks dispatch rates mathematically prevent aggregate requests from exceeding the 15 RPM free-tier quota ceiling across any number of Cloud Run instances.
