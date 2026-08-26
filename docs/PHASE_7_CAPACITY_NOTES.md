# RecoveryOS Capacity & Degradation Engineering Notes

This document defines the operational capacity envelope, performance bounds, concurrency constraints, and graceful degradation strategies for RecoveryOS.

---

## 1. Production Resource Specifications

| Service Component | CPU Limits | Memory Limits | Container Concurrency | Request Timeout | Max Instances | Min Instances |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **API Service (`recoveryos`)** | 1 vCPU | 512 MiB | 80 requests/container | 300 seconds | 1 instance | 1 instance |
| **Worker Service (`recoveryos-worker`)** | 1 vCPU | 512 MiB | 80 requests/container | 300 seconds | 1 instance | 1 instance |

---

## 2. Subsystem Capacity & Throughput Bounds

### A. Cloud Run Container Processing
- **Current Configuration**: With `max-instances=1` and `concurrency=80`, a single container instance handles all traffic.
- **Safe Request Envelope**: Up to 40 sustained requests/second on the API edge with p95 response time $< 15\text{ms}$ for read/probes and $< 120\text{ms}$ for Firestore writes.
- **Memory Footprint**: Baseline memory usage is $\approx 110\text{MiB}$. Under load, memory peaks at $\approx 190\text{MiB}$, well within the $512\text{MiB}$ ceiling.

### B. Pub/Sub Ingestion & Delivery Pacing
- **Push Subscription Throughput**: Push delivery pushes messages to `recoveryos-worker` up to Cloud Run container concurrency (80 concurrent pushes max per instance).
- **Ack Deadline**: Configured to **60 seconds**.
- **Delivery Attempts**: Max **5 attempts** before routing unacknowledged messages to `recoveryos-workflow-execution-dlq`.

### C. Google Cloud Firestore Write Contention Rules
- **Document Contention Rule**: Firestore supports up to **1 write per second per individual document**.
- **Multi-Worker Safety**: Because each workflow document has a unique `workflow_id` document path (`workflows/{workflow_id}` and `idempotency/{idempotency_key}`), workflows scale horizontally across independent documents without encountering document write bottlenecks.
- **Gemini Quota Coordination**: The distributed Gemini rate limiter coordinates LLM call pacing through a dedicated lease document with a minimum interval spacing of **6.5 seconds**, preventing 429 quota exhaustion across concurrent workers.

---

## 3. Thundering-Herd & Duplicate Amplification Avoidance

1. **Idempotency Lease Protection**:
   - When multiple push deliveries arrive simultaneously for the same `idempotency_key`, only the first worker transaction successfully acquires `status: PENDING`.
   - Subsequent concurrent deliveries immediately observe the existing lease and exit cleanly with `SKIPPED_DUPLICATE` without invoking agent reasoning or mutating state.
2. **Deterministic Backoff on OCC Conflicts**:
   - If two workers attempt state transition on the same workflow, the out-of-order worker receives `StaleWorkflowStateError` and emits a NACK.
   - Pub/Sub applies exponential backoff (1s minimum, 60s maximum) to decouple retry attempts.

---

## 4. Graceful Degradation Protocols

| Degradation Trigger | System Reaction | User / Client Impact | Recovery Path |
|:---|:---|:---|:---|
| **High API Traffic Load ($>80$ concurrent)** | Cloud Run queues incoming requests up to timeout (300s) | Minor latency increase on dispatch ($+50\text{ms}$) | Requests drain smoothly; autoscale instances if sustained |
| **Pub/Sub Transport Delay** | API returns 202 immediately; message waits in queue | Asynchronous background execution delayed | Worker processes queue sequentially as capacity frees |
| **Gemini Rate Limit / 429** | Gemini Circuit Breaker transitions to `OPEN` | Workflows pause dispatch and wait for cooldown | Circuit resets to `HALF_OPEN` and resumes execution |
| **Worker OOM / Crash** | Container terminates; Pub/Sub redelivers message | Execution delayed by one retry cycle ($\approx 10\text{s}$) | Clean container restarts within 2 seconds |
