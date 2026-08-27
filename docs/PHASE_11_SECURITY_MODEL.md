# RecoveryOS Phase 11 Security Model & Threat Matrix

**Document Status**: ACTIVE & ENFORCED  
**Security Classification**: CONFIDENTIAL — RECOVERYOS SYSTEM ARCHITECTURE  

---

## 1. Authentication & Identity Boundaries

### 1.1 Credential Verification & Role Binding
- **Hash Function**: PBKDF2-HMAC-SHA256 with 100,000 iterations and 16-byte random salt per user.
- **Constant-Time Verification**: `hmac.compare_digest` prevents side-channel timing attacks.
- **Server-Side Authority**: Identity credentials derive `Role` (`VIEWER`, `OPERATOR`, `APPROVER`, `ADMIN`) and `tenant_id` exclusively from verified server-side `UserRecord` storage.
- **Elevation Defense**: Any client attempting to specify `role="admin"` or an arbitrary `tenant_id` in login payloads is strictly ignored; only configured entitlements are encoded into the signed JWT.

### 1.2 JWT Token Specifications
- **Algorithm**: HMAC-SHA256 (`HS256`).
- **Signature Secret**: Validated to be >= 32 characters in production via `validate_production_config()`.
- **Claims**:
  - `sub`: Authenticated username
  - `role`: Canonical role enum (`admin`, `operator`, `approver`, `viewer`)
  - `tenant_id`: Multi-tenant boundary
  - `iat`: Timestamp of issue
  - `exp`: Timestamp of expiration (default 60 minutes)

---

## 2. Server-Sent Events (SSE) Security Architecture

### 2.1 Single-Use Ticket Protocol
```
Client (Browser)                 RecoveryOS API                 SSETicketStore
       │                                │                             │
       ├─── 1. POST /api/auth/sse-ticket ─────────────────────────────►│
       │    (Authorization: Bearer <JWT>, workflow_id="wf-123")        │
       │                                │                             │
       │                                ├── 2. Verify Principal &     │
       │                                │      Tenant Access          │
       │                                │                             │
       │                                ├── 3. Issue 60s Single-Use   │
       │                                │      Random Ticket          │
       │                                │      ("sset_<hex>")         │
       │                                │                             │
       │◄── 4. Return Ticket ID ────────┴─────────────────────────────┤
       │                                                              │
       ├─── 5. GET /api/workflows/wf-123/events/stream?ticket=sset_.. ►│
       │                                │                             │
       │                                ├── 6. Atomically Consume &   │
       │                                │      Invalidate Ticket      │
       │                                │                             │
       │◄── 7. Stream Authenticated Events (text/event-stream) ───────┤
```

### 2.2 Attack Mitigations
- **URL Leakage Defense**: Raw JWTs are never embedded in URL query parameters.
- **Replay Attack Defense**: Tickets are consumed atomically on first connection; second connection attempts with the same ticket fail with HTTP 401.
- **Workflow / Tenant Boundary**: Tickets are cryptographically validated against the specific target `workflow_id` and tenant.

---

## 3. Concurrency & Multi-Worker Coordination

### 3.1 Distributed Operation Claim & Lease Heartbeat
- **Claim Acquisition**: Atomic 60-second lease in Firestore transaction (`_claim_tx`) or in-memory lock.
- **Background Heartbeat**: `WorkflowEventConsumer` renews the lease every 20 seconds during LLM agent execution.
- **Ownership Invariants**: Only the active worker ID can renew the lease. Attempted renewals on expired or completed claims fail closed.
