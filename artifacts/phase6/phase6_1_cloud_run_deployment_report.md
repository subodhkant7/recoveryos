# Phase 6.1: Google Cloud Run Production Deployment Report

---

## 1. Executive Summary & Deployment Status

### **PHASE 6.1 STATUS: PASS**

RecoveryOS is successfully deployed to Google Cloud Run in `asia-east1` as an authenticated, production-hardened microservice backed by Google Cloud Firestore, Secret Manager, and Google Gemini.

The deployment failure in Revision `recoveryos-00003-sqz` has been resolved. Revision `recoveryos-00004-sw7` is currently **READY (100% Traffic)** and passing all operational probes.

---

## 2. Root Cause Analysis of Previous Failure

### A. Root Cause 1: CPU Architecture Binary Mismatch (`exec format error`)
- **Diagnosis:** The initial image was built locally on an Apple Silicon macOS host (`linux/arm64`) and pushed to Artifact Registry. When Google Cloud Run launched the container on standard `linux/amd64` instances, the Linux kernel threw `exec format error` (ENOEXEC) when attempting to execute `/usr/local/bin/uvicorn`.
- **Resolution:** Rebuilt the production image via Google Cloud Build (`gcloud builds submit`), compiling natively on `linux/amd64` architecture.

### B. Root Cause 2: Hardcoded Port vs. Dynamic `$PORT` Ingestion
- **Diagnosis:** The Dockerfile CMD used JSON exec array form `["uvicorn", "backend.api.server:app", "--host", "0.0.0.0", "--port", "8000"]`, preventing shell expansion of Cloud Run's dynamic `$PORT` environment variable.
- **Resolution:** Updated Dockerfile CMD to `["sh", "-c", "exec uvicorn backend.api.server:app --host 0.0.0.0 --port ${PORT:-8000}"]`. The `exec` prefix ensures `SIGTERM` signals are delivered directly to the Uvicorn master process for clean 5-second graceful task draining.

### C. Root Cause 3: Missing Production Secrets in Cloud Run Environment
- **Diagnosis:** When `ENVIRONMENT=production`, RecoveryOS enforces fail-closed validation (`backend.config.validate_production_config()`). The previous deployment did not mount `JWT_SECRET_KEY` or `GEMINI_API_KEY` from Secret Manager, nor did it define `CORS_ALLOW_ORIGINS`.
- **Resolution:** Configured Cloud Run to mount Secret Manager secrets (`recoveryos-jwt-secret:latest` $\rightarrow$ `JWT_SECRET_KEY`, `recoveryos-gemini-key:latest` $\rightarrow$ `GEMINI_API_KEY`) and set `CORS_ALLOW_ORIGINS=https://recoveryos-321161003794.asia-east1.run.app`.

---

## 3. Deployed Cloud Run Service Specification

| Property | Value |
| :--- | :--- |
| **GCP Project** | `recoveryos-506713` (Project Number: `321161003794`) |
| **Region** | `asia-east1` |
| **Service Name** | `recoveryos` |
| **Active Revision** | `recoveryos-00004-sw7` |
| **Service URL** | `https://recoveryos-321161003794.asia-east1.run.app` |
| **Container Image** | `asia-east1-docker.pkg.dev/recoveryos-506713/recoveryos/recoveryos:phase6-1` |
| **Digest** | `sha256:9f1b1beffe0e6a1b974f8f88c74dbfc52fc41bbb5d507a3677d91932784ee1ea` |
| **Runtime Service Account** | `recoveryos-runtime@recoveryos-506713.iam.gserviceaccount.com` |
| **IAM Access Policy** | `--no-allow-unauthenticated` (Cloud Run IAM Protected) |
| **Scaling Policy** | `min-instances=1`, `max-instances=1` (Preserving single-process Gemini quota safety) |
| **Resources** | 1.0 vCPU, 512 MiB RAM, 300s request timeout |
| **Persistence Backend** | `firestore` (Native GCP Firestore via ADC) |

---

## 4. Live Verification Evidence

### A. Cloud Run IAM Access Control
- **Unauthenticated Request:**
  ```http
  GET https://recoveryos-321161003794.asia-east1.run.app/api/health
  HTTP/2 403 Forbidden
  server: Google Frontend
  ```
  *(Confirmed: Public unauthenticated requests are strictly rejected at the Google edge proxy).*

### B. Authenticated Endpoint Probes
- **`GET /api/health`:**
  ```json
  {
    "status": "healthy",
    "service": "recoveryos",
    "timestamp": "2026-08-26T16:11:24.099914+00:00",
    "model": "gemini-3.5-flash",
    "environment": "production"
  }
  ```
- **`GET /api/ready`:**
  ```json
  {
    "status": "ready",
    "service": "recoveryos",
    "timestamp": "2026-08-26T16:11:24.444651+00:00",
    "persistence_backend": "firestore"
  }
  ```
- **`GET /metrics`:**
  ```prometheus
  # TYPE recoveryos_http_requests_total counter
  recoveryos_http_requests_total{endpoint="/api/health",method="GET",status="200"} 1.0
  recoveryos_http_requests_total{endpoint="/api/ready",method="GET",status="200"} 1.0
  ```

---

## 5. Security & Multi-Replica Quota Guardrails

1. **Secret Redaction:** No secret values are hardcoded in source code or visible in logs. Secrets are mounted directly as environment variables from Secret Manager.
2. **Deterministic Sovereignty:** Policy enforcement, outcome verification contracts, and idempotency guarantees execute identically in Cloud Run.
3. **Multi-Replica Gemini Quota Constraint:** `maxScale: '1'` is strictly enforced on the Cloud Run service, preserving the single-process `ResilientGemini` token queue ($\le 10\text{ RPM}$) until Phase 6.2 introduces distributed Redis rate limiting.

---

## 6. Regression Testing Battery
- **Deterministic Battery:** 139 / 139 PASSED (18 test files)
- **Live Firestore Tests:** 3 / 3 PASSED
- **Container Contract:** 2 / 2 PASSED
- **Overall Status:** **100% GREEN**
