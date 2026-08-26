# Phase 6.2: Asynchronous Message Contract & Schema Specification

---

## 1. Overview & Protocol Principles
In RecoveryOS Phase 6.2, workflow dispatch and step execution are decoupled from synchronous HTTP request handling via Google Cloud Pub/Sub.

Because Google Cloud Pub/Sub provides **at-least-once delivery**, every message contract must adhere to these strict invariants:
1. **Idempotent Dispatch:** Re-delivering an identical message must not cause duplicate external mutations or state corruption.
2. **Deterministic Routing:** Messages contain canonical identifiers (`workflow_id`, `tenant_id`, `step_id`, `target_version`) allowing workers to perform OCC verification before execution.
3. **Traceability & Correlation:** Contextual IDs (`trace_id`, `request_id`, `correlation_id`) must propagate across message headers and payload attributes.

---

## 2. Topic & Subscription Topology

```
+----------------------------------------------------------------------------------------------------+
|                                    Google Cloud Pub/Sub Topology                                   |
+----------------------------------------------------------------------------------------------------+
                                                  |
           [ API Service / Dispatcher ]           |
                        |                         |
         publish(workflow.dispatch.v1)            |
                        v                         |
     +------------------------------------+       |
     | TOPIC: recoveryos-workflow-events  |       |
     +------------------------------------+       |
           |                        |             |
           v (Filter: type=DISPATCH)|             |
+-------------------------------------+           |
| SUB: recoveryos-worker-subscription |           |
| Push / Pull to Worker Fleet         |           |
+-------------------------------------+           |
           |                                      |
     (Max Retries: 5)                             |
           v                                      |
+--------------------------------------+          |
| TOPIC: recoveryos-workflow-deadletter|          |
+--------------------------------------+          |
           |                                      |
+--------------------------------------+          |
| SUB: recoveryos-deadletter-sub       |          |
| (Operator Alerting & Forensics)      |          |
+--------------------------------------+          |
```

---

## 3. Message Schema Definition: `WorkflowExecutionMessage`

### A. Attributes (Pub/Sub Message Headers)
| Attribute Key | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `message_id` | String (UUID) | Yes | Globally unique message ID for deduplication |
| `event_type` | String | Yes | `WORKFLOW_DISPATCH`, `STEP_EXECUTE`, `APPROVAL_RESUME` |
| `schema_version` | String | Yes | Version format identifier (e.g. `1.0.0`) |
| `tenant_id` | String | Yes | Tenant isolation identifier (e.g. `tenant-acme`) |
| `workflow_id` | String (UUID) | Yes | Target workflow ID |
| `expected_version` | Integer | Yes | OCC state version expected prior to execution |
| `correlation_id` | String (UUID) | Yes | Request correlation ID for distributed tracing |
| `published_at` | String (ISO-8601) | Yes | UTC timestamp of dispatch |

### B. Payload (JSON Body)
```json
{
  "schema_version": "1.0.0",
  "message_id": "msg-8f92b714-9988-4c12-b5cf-795a12ef6201",
  "event_type": "WORKFLOW_DISPATCH",
  "published_at": "2026-08-26T16:30:00.000000Z",
  "correlation_id": "req-9a1b2c3d-4e5f-6789-0123-abcdef456789",
  "workflow": {
    "workflow_id": "wf-cff6ac0f-bd78-4a59-9639-24c470f74f14",
    "tenant_id": "tenant-prod-exec",
    "scenario_name": "billing_unavailable",
    "expected_version": 1,
    "target_state": "EXECUTING"
  },
  "context": {
    "initiated_by": "user-operator-1",
    "actor_role": "OPERATOR",
    "trace_parent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
  }
}
```

---

## 4. Message Lifecycle & State Machine Transitions

| Message Event | Trigger Condition | Worker Action | State Transition | Terminal Behavior |
| :--- | :--- | :--- | :--- | :--- |
| `WORKFLOW_DISPATCH` | API `POST /api/scenarios/{name}` | Acquire worker claim, load workflow snapshot, invoke Taskmaster | `CREATED` $\rightarrow$ `EXECUTING` | ACK on completion / approval wait |
| `APPROVAL_RESUME` | API `POST /approve` | Verify approval record in Firestore, resume agent execution | `AWAITING_APPROVAL` $\rightarrow$ `EXECUTING` | ACK on completion |
| `REDELIVERY_DUPLICATE` | Pub/Sub at-least-once replay | OCC version or OperationClaim check detects already active/completed | No-op; return cached result | Instant ACK |
| `OCC_CONFLICT` | Worker B already updated version | Reject stale write, re-fetch latest snapshot or drop duplicate | None / Reload | ACK (Drop duplicate) or Re-enqueue |
| `RETRYABLE_FAILURE` | Gemini 429 / Transient HTTP 503 | Worker applies exponential backoff; if exceeded, NACK | None $\rightarrow$ Retry | Cloud Pub/Sub backoff |
| `POISON_MESSAGE` | Schema validation error / Corrupt payload | Discard malformed message; write audit log | Transition to `ESCALATED` / Dead-letter | ACK & Send to Dead-Letter Topic |
| `TERMINAL_WORKFLOW` | Message targeting completed/escalated wf | Engine rejects transition (`VALID_TRANSITIONS`) | Immutable (`COMPLETED` / `ESCALATED`) | Instant ACK |

---

## 5. Poison & Dead-Letter Policy
1. **Dead-Letter Threshold:** `max_delivery_attempts = 5`.
2. **Dead-Letter Forwarding:** Dead-lettered messages are automatically routed to `recoveryos-workflow-deadletter`.
3. **Dead-Letter Subscription:** Monitored via Cloud Monitoring alert policy; alerts on `deadletter_messages_count > 0`.
