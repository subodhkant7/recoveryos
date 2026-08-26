# Phase 6.2: Infrastructure & Cost Management Plan

---

## 1. Google Cloud Resources to Provision in Phase 6.2

| Resource Type | Resource Name | Purpose | Configuration / SKU | Estimated Monthly Cost |
| :--- | :--- | :--- | :--- | :--- |
| **Cloud Pub/Sub Topic** | `recoveryos-workflow-events` | Ingestion of workflow dispatch and resume events | Standard Regional Topic (`asia-east1`) | **$0.00** (Free Tier covers 10 GB/month) |
| **Cloud Pub/Sub Dead-Letter Topic** | `recoveryos-workflow-deadletter` | Dead-letter storage for unparseable or retry-exhausted messages | Standard Regional Topic (`asia-east1`) | **$0.00** (Included in Free Tier) |
| **Cloud Pub/Sub Push Subscription** | `recoveryos-worker-sub` | Push subscriber delivering events to Cloud Run Worker service | Push to `/api/pubsub/consume`, `ack_deadline: 60s`, `max_delivery_attempts: 5` | **$0.00** (Included in Free Tier) |
| **Cloud Pub/Sub Dead-Letter Sub** | `recoveryos-deadletter-sub` | Pull subscription for dead-letter messages and alerts | Standard Pull Subscription | **$0.00** (Included in Free Tier) |
| **Cloud Tasks Queue** | `recoveryos-gemini-queue` | Multi-worker rate limiter for Gemini RPM quota safety | `max_dispatches_per_second: 0.25`, `max_concurrent_dispatches: 1` | **$0.00** (Free Tier covers 1M operations/month) |
| **Cloud Run Worker Service** | `recoveryos-worker` | Dedicated asynchronous background worker fleet | 1.0 vCPU, 512 MiB RAM, `min-instances: 0`, `max-instances: 2` | **$0.00 – $3.00** (Included in Cloud Run free tier) |
| **Cloud Run API Service** | `recoveryos` | Existing API ingress service | 1.0 vCPU, 512 MiB RAM, `min-instances: 1`, `max-instances: 1` | **~$5.00** (Baseline running cost) |
| **Cloud Monitoring Alert Policy** | `alert-recoveryos-deadletter` | Instant alert on any dead-lettered message | Metric alert on `deadletter_messages_count > 0` | **$0.00** (Standard monitoring free tier) |

---

## 2. Memorystore Redis Comparison & Elimination

| Cost Category | Memorystore Redis Option | Recommended Cloud Tasks + Firestore Option | Savings |
| :--- | :--- | :--- | :--- |
| **Instance Cost** | ~$35.00 / month (Basic 1 GB) | $0.00 (Serverless Cloud Tasks free tier) | **-$35.00 / month** |
| **VPC Connector** | ~$15.00 / month (Serverless VPC Access) | $0.00 (No VPC Connector required) | **-$15.00 / month** |
| **Operational Overhead** | Manual patching, maintenance windows, connection pool sizing | Zero operational overhead (100% serverless managed) | **Significant time savings** |
| **Total Cost Delta** | **~$50.00 / month** | **~$0.00 – $5.00 / month** | **$50.00 / month (100% serverless)** |

---

## 3. Financial Invariant Guarantee
- **No Bill Shocks:** All provisioned components operate well within Google Cloud free-tier allocations for development and low-volume operations.
- **Scale-to-Zero:** The asynchronous worker service scales down to 0 instances when no workflow execution messages are pending in Pub/Sub.
