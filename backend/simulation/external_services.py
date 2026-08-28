"""
Simulated external services for ACME Corp onboarding demo.

These simulate real external APIs (identity verification, billing, etc.)
with realistic response shapes and external idempotency reconciliation.

Each service checks the FailureInjector to determine if it should fail
for this specific call.

Authoritative external state tracks mutations so that RecoveryOS can
reconcile state across crashes and retries.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from backend.simulation.failure_injector import FailureInjector


class SimulatedServices:
    """
    Container for all simulated external services.

    Each method simulates an external API call. The failure injector
    can cause any of them to fail in configurable ways.
    """

    def __init__(self, failure_injector: FailureInjector):
        self._injector = failure_injector
        # In-memory state for simulated services (acts as external DBs)
        self._identity_records: dict[str, dict] = {}
        self._document_records: dict[str, dict] = {}
        self._risk_records: dict[str, dict] = {}
        self._billing_records: dict[str, dict] = {}
        self._account_records: dict[str, dict] = {}
        self._notification_records: dict[str, dict] = {}
        # Global idempotency registry in external world (operation index)
        self._operations_by_key: dict[str, dict] = {}
        # Service availability (can be affected by failure injection)
        self._service_status: dict[str, dict] = {
            "identity": {"status": "healthy", "latency_ms": 120},
            "documents": {"status": "healthy", "latency_ms": 200},
            "risk": {"status": "healthy", "latency_ms": 350},
            "billing_stripe": {"status": "healthy", "latency_ms": 180},
            "billing_paypal": {"status": "healthy", "latency_ms": 220},
            "billing_square": {"status": "healthy", "latency_ms": 250},
            "accounts": {"status": "healthy", "latency_ms": 90},
            "notifications": {"status": "healthy", "latency_ms": 150},
        }

    @property
    def failure_injector(self) -> FailureInjector:
        return self._injector

    # ------------------------------------------------------------------
    # Authoritative External State Reconciliation
    # ------------------------------------------------------------------

    async def check_external_mutation(
        self,
        tool_name: str,
        idempotency_key: str,
        customer_id: str,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """
        Query authoritative external state to check if mutation already occurred.

        Used when local state is unknown or after process crash.
        """
        # 1. Direct idempotency key match
        if idempotency_key in self._operations_by_key:
            rec = self._operations_by_key[idempotency_key]
            return {**rec, "status": "success", "reconciled": True}

        # 2. Domain-specific entity inspection
        if tool_name == "setup_billing":
            rec = self._billing_records.get(customer_id)
            if rec:
                provider = kwargs.get("provider", "stripe")
                plan_tier = kwargs.get("plan_tier", "enterprise")
                billing_cycle = kwargs.get("billing_cycle", "monthly")
                if (
                    rec.get("provider") == provider
                    and rec.get("plan_tier") == plan_tier
                    and rec.get("billing_cycle") == billing_cycle
                ):
                    return {**rec, "status": "success", "reconciled": True}

        elif tool_name == "verify_identity":
            rec = self._identity_records.get(customer_id)
            if rec:
                return {**rec, "status": "success", "reconciled": True}

        elif tool_name == "validate_documents":
            rec = self._document_records.get(customer_id)
            if rec:
                return {**rec, "status": "success", "reconciled": True}

        elif tool_name == "run_risk_check":
            rec = self._risk_records.get(customer_id)
            if rec:
                return {**rec, "status": "success", "reconciled": True}

        elif tool_name == "activate_account":
            rec = self._account_records.get(customer_id)
            if rec:
                return {**rec, "status": "success", "reconciled": True}

        elif tool_name == "send_welcome_package":
            rec = self._notification_records.get(customer_id)
            if rec:
                return {**rec, "status": "success", "reconciled": True}

        return None

    # ------------------------------------------------------------------
    # Identity Verification
    # ------------------------------------------------------------------

    async def verify_identity(
        self, workflow_id: str, customer_id: str, id_type: str = "government",
        full_name: str = "", idempotency_key: str = "", **kwargs: Any,
    ) -> dict[str, Any]:
        """Simulate a government ID verification API."""
        if idempotency_key and idempotency_key in self._operations_by_key:
            return {**self._operations_by_key[idempotency_key], "status": "success"}

        existing = self._identity_records.get(customer_id)
        if existing:
            return {**existing, "status": "success"}

        failure = await self._injector.check_failure(
            workflow_id, "verify_identity"
        )
        if failure:
            return failure

        ref_id = f"idv-{uuid.uuid4().hex[:8]}"
        record = {
            "verification_reference_id": ref_id,
            "customer_id": customer_id,
            "id_type": id_type,
            "full_name": full_name,
            "verification_status": "verified",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        self._identity_records[customer_id] = record
        if idempotency_key:
            self._operations_by_key[idempotency_key] = record
        return {**record, "status": "success"}

    async def query_identity_status(
        self, customer_id: str,
    ) -> dict[str, Any]:
        """Independent verification: query identity service by customer ID."""
        record = self._identity_records.get(customer_id)
        if not record:
            return {"found": False, "status": "not_found", "customer_id": customer_id}
        return {"found": True, **record}

    # ------------------------------------------------------------------
    # Document Validation
    # ------------------------------------------------------------------

    async def validate_documents(
        self, workflow_id: str, customer_id: str,
        document_types: list[str] | None = None, idempotency_key: str = "", **kwargs: Any,
    ) -> dict[str, Any]:
        """Simulate a document OCR/validation service."""
        if idempotency_key and idempotency_key in self._operations_by_key:
            return {**self._operations_by_key[idempotency_key], "status": "success"}

        existing = self._document_records.get(customer_id)
        if existing:
            return {**existing, "status": "success"}

        failure = await self._injector.check_failure(
            workflow_id, "validate_documents"
        )
        if failure:
            return failure

        sub_id = f"doc-{uuid.uuid4().hex[:8]}"
        record = {
            "validation_reference_id": sub_id,
            "customer_id": customer_id,
            "document_types": document_types or ["incorporation", "tax_id"],
            "validation_status": "validated",
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._document_records[customer_id] = record
        if idempotency_key:
            self._operations_by_key[idempotency_key] = record
        return {**record, "status": "success"}

    async def query_document_status(
        self, customer_id: str,
    ) -> dict[str, Any]:
        """Independent verification: query document service by customer ID."""
        record = self._document_records.get(customer_id)
        if not record:
            return {"found": False, "status": "not_found", "customer_id": customer_id}
        return {"found": True, **record}

    # ------------------------------------------------------------------
    # Risk Assessment
    # ------------------------------------------------------------------

    async def run_risk_check(
        self, workflow_id: str, customer_id: str, idempotency_key: str = "", **kwargs: Any,
    ) -> dict[str, Any]:
        """Simulate a credit/risk scoring engine."""
        if idempotency_key and idempotency_key in self._operations_by_key:
            return {**self._operations_by_key[idempotency_key], "status": "success"}

        existing = self._risk_records.get(customer_id)
        if existing:
            return {**existing, "status": "success"}

        failure = await self._injector.check_failure(
            workflow_id, "run_risk_check"
        )
        if failure:
            return failure

        assessment_id = f"risk-{uuid.uuid4().hex[:8]}"
        record = {
            "assessment_id": assessment_id,
            "customer_id": customer_id,
            "risk_score": 32,  # Low risk
            "risk_level": "low",
            "assessment_status": "completed",
            "assessed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._risk_records[customer_id] = record
        if idempotency_key:
            self._operations_by_key[idempotency_key] = record
        return {**record, "status": "success"}

    async def query_risk_status(
        self, customer_id: str,
    ) -> dict[str, Any]:
        """Independent verification: query risk service by customer ID."""
        record = self._risk_records.get(customer_id)
        if not record:
            return {"found": False, "status": "not_found", "customer_id": customer_id}
        return {"found": True, **record}

    # ------------------------------------------------------------------
    # Billing Setup (single tool, provider as parameter)
    # ------------------------------------------------------------------

    async def setup_billing(
        self, workflow_id: str, customer_id: str,
        provider: str = "stripe",
        plan_tier: str = "enterprise",
        billing_cycle: str = "monthly",
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Simulate billing API.

        Guarantees that a repeat operation returns the existing subscription
        instead of creating a duplicate subscription.
        """
        if idempotency_key and idempotency_key in self._operations_by_key:
            return {**self._operations_by_key[idempotency_key], "status": "success"}

        service_key = f"billing_{provider}"
        if service_key not in self._service_status:
            return {"status": "error", "error_type": "UNKNOWN_PROVIDER", "message": f"Billing provider '{provider}' is not supported"}

        # Check failure injector FIRST before any cached state
        failure = await self._injector.check_failure(
            workflow_id, "setup_billing", context={"provider": provider}
        )
        if failure:
            if failure.get("status") == "error":
                service_key = f"billing_{provider}"
                if service_key in self._service_status:
                    self._service_status[service_key]["status"] = "down"
                return failure
            else:
                record = {**failure, "customer_id": customer_id}
                self._billing_records[customer_id] = record
                if idempotency_key:
                    self._operations_by_key[idempotency_key] = record
                return {**record, "status": "success"}

        existing = self._billing_records.get(customer_id)
        if (
            existing
            and existing.get("provider") == provider
            and existing.get("plan_tier") == plan_tier
            and existing.get("billing_cycle") == billing_cycle
        ):
            return {**existing, "status": "success"}

        subscription_id = f"sub-{uuid.uuid4().hex[:8]}"
        record = {
            "subscription_id": subscription_id,
            "customer_id": customer_id,
            "provider": provider,
            "plan_tier": plan_tier,
            "billing_cycle": billing_cycle,
            "subscription_status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._billing_records[customer_id] = record
        if idempotency_key:
            self._operations_by_key[idempotency_key] = record
        return {**record, "status": "success"}

    async def query_billing_status(
        self, customer_id: str,
    ) -> dict[str, Any]:
        """Independent verification: query billing for active subscription."""
        record = self._billing_records.get(customer_id)
        if not record:
            return {"found": False, "status": "not_found", "customer_id": customer_id}
        return {"found": True, **record}

    # ------------------------------------------------------------------
    # Account Activation
    # ------------------------------------------------------------------

    async def activate_account(
        self, workflow_id: str, customer_id: str, idempotency_key: str = "", **kwargs: Any,
    ) -> dict[str, Any]:
        """Simulate internal account activation."""
        if idempotency_key and idempotency_key in self._operations_by_key:
            return {**self._operations_by_key[idempotency_key], "status": "success"}

        existing = self._account_records.get(customer_id)
        if existing and existing.get("account_status") == "active":
            return {**existing, "status": "success", "already_active": True}

        failure = await self._injector.check_failure(
            workflow_id, "activate_account"
        )
        if failure:
            return failure

        account_id = f"acct-{uuid.uuid4().hex[:8]}"
        record = {
            "account_id": account_id,
            "customer_id": customer_id,
            "account_status": "active",
            "activation_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._account_records[customer_id] = record
        if idempotency_key:
            self._operations_by_key[idempotency_key] = record
        return {**record, "status": "success"}

    async def query_account_status(
        self, customer_id: str,
    ) -> dict[str, Any]:
        """Independent verification: query account status."""
        record = self._account_records.get(customer_id)
        if not record:
            return {"found": False, "status": "not_found", "customer_id": customer_id}
        return {"found": True, **record}

    # ------------------------------------------------------------------
    # Welcome Package
    # ------------------------------------------------------------------

    async def send_welcome_package(
        self, workflow_id: str, customer_id: str,
        email: str = "", idempotency_key: str = "", **kwargs: Any,
    ) -> dict[str, Any]:
        """Simulate sending a welcome email/notification."""
        if idempotency_key and idempotency_key in self._operations_by_key:
            return {**self._operations_by_key[idempotency_key], "status": "success"}

        existing = self._notification_records.get(customer_id)
        if existing and existing.get("delivery_status") == "delivered":
            return {**existing, "status": "success"}

        failure = await self._injector.check_failure(
            workflow_id, "send_welcome_package"
        )
        if failure:
            return failure

        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        record = {
            "message_id": message_id,
            "customer_id": customer_id,
            "email": email,
            "delivery_status": "delivered",
            "delivery_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._notification_records[customer_id] = record
        if idempotency_key:
            self._operations_by_key[idempotency_key] = record
        return {**record, "status": "success"}

    async def query_notification_status(
        self, message_id: str,
    ) -> dict[str, Any]:
        """Independent verification: query notification delivery status."""
        for record in self._notification_records.values():
            if record.get("message_id") == message_id:
                return {"found": True, **record}
        return {"found": False, "status": "not_found", "message_id": message_id}

    # ------------------------------------------------------------------
    # Service Status (read-only diagnostic)
    # ------------------------------------------------------------------

    def get_service_status(self, service_name: str) -> dict[str, Any]:
        """Check the health/availability of an external service."""
        status = self._service_status.get(service_name)
        if not status:
            return {
                "service": service_name,
                "status": "unknown",
                "message": f"No status information for '{service_name}'",
            }
        return {"service": service_name, **status}

    def get_all_service_status(self) -> dict[str, dict]:
        """Return status of all known services."""
        return {k: {"service": k, **v} for k, v in self._service_status.items()}

    def configure_billing_provider(
        self, provider: str, status: str = "healthy", supported_plan_tiers: list[str] | None = None,
    ) -> None:
        """Dynamic configuration of billing provider status and capabilities for simulated environments."""
        service_key = f"billing_{provider}"
        self._service_status[service_key] = {"status": status, "latency_ms": 150}
        if supported_plan_tiers is not None:
            if not hasattr(self, "_provider_capabilities"):
                self._provider_capabilities = {}
            self._provider_capabilities[provider] = supported_plan_tiers

    def list_available_billing_providers(self) -> list[dict[str, Any]]:
        """
        Discover available billing providers and their capabilities.
        """
        providers = []
        for key, status in self._service_status.items():
            if key.startswith("billing_"):
                provider_name = key.replace("billing_", "")
                default_tiers = (
                    ["starter", "professional", "enterprise"]
                    if provider_name in ("stripe", "paypal")
                    else ["starter", "professional"]
                )
                custom_tiers = getattr(self, "_provider_capabilities", {}).get(provider_name, default_tiers)
                providers.append({
                    "provider": provider_name,
                    "status": status["status"],
                    "supports_enterprise": "enterprise" in custom_tiers,
                    "supports_monthly_billing": True,
                    "supported_plan_tiers": custom_tiers,
                })
        return providers
