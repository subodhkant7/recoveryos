"""
Failure injection mechanism.

Reads failure configuration from an in-memory store (or Firestore in
production). Each scenario configures which tool calls should fail and how.

The agent code has ZERO knowledge of what failures will be injected.
Failure configs are set when a scenario is launched. The injector sits
inside the simulated services layer — it is invisible to the agent.

NOT hard-coded if/else for specific failures.
The agent genuinely discovers the failure and reasons about alternatives.
"""

from __future__ import annotations

from typing import Any


class CrashBeforePersistenceError(Exception):
    """Simulated worker crash occurring after external mutation succeeded but before local persistence."""

    pass


class FailureConfig:
    """Configuration for a single failure injection."""

    def __init__(
        self,
        tool_name: str,
        failure_type: str,
        error_response: dict[str, Any],
        remaining_count: int = 1,
        condition: dict[str, Any] | None = None,
        affects_status_check: bool = False,
    ):
        self.tool_name = tool_name
        self.failure_type = failure_type
        self.error_response = error_response
        self.remaining_count = remaining_count
        self.condition = condition or {}
        self.affects_status_check = affects_status_check


class CrashConfig:
    """Configuration for simulated crash-after-success."""

    def __init__(self, tool_name: str, remaining_count: int = 1):
        self.tool_name = tool_name
        self.remaining_count = remaining_count


class FailureInjector:
    """
    Manages failure injection for demo scenarios and crash testing.

    Failure configs are keyed by (workflow_id, tool_name).
    Each config has a remaining_count that decrements on each failure.
    When remaining_count reaches 0, the tool succeeds normally.
    """

    def __init__(self) -> None:
        # Key: (workflow_id, tool_name) → list of FailureConfig
        self._configs: dict[tuple[str, str], list[FailureConfig]] = {}
        # Key: (workflow_id, tool_name) → list of CrashConfig
        self._crash_configs: dict[tuple[str, str], list[CrashConfig]] = {}

    def configure_failure(
        self,
        workflow_id: str,
        tool_name: str,
        failure_type: str,
        error_response: dict[str, Any],
        remaining_count: int = 1,
        condition: dict[str, Any] | None = None,
        affects_status_check: bool = False,
    ) -> None:
        """Register a failure injection for a specific workflow + tool."""
        key = (workflow_id, tool_name)
        if key not in self._configs:
            self._configs[key] = []
        self._configs[key].append(
            FailureConfig(
                tool_name=tool_name,
                failure_type=failure_type,
                error_response=error_response,
                remaining_count=remaining_count,
                condition=condition,
                affects_status_check=affects_status_check,
            )
        )

    def configure_crash_after_external_success(
        self,
        workflow_id: str,
        tool_name: str,
        remaining_count: int = 1,
    ) -> None:
        """Register a crash simulation that triggers immediately after external success."""
        key = (workflow_id, tool_name)
        if key not in self._crash_configs:
            self._crash_configs[key] = []
        self._crash_configs[key].append(
            CrashConfig(tool_name=tool_name, remaining_count=remaining_count)
        )

    def has_failure_config(
        self,
        workflow_id: str,
        tool_name: str,
    ) -> bool:
        """Return whether a failure injection was already configured.

        Demo scenarios are configured by both the API and the worker. This guard
        keeps a one-time failure deterministic instead of re-injecting the
        same fault on every recovery dispatch (e.g. APPROVAL_RESUME).
        """
        return bool(self._configs.get((workflow_id, tool_name)))

    def has_crash_after_external_success_config(
        self,
        workflow_id: str,
        tool_name: str,
    ) -> bool:
        """Return whether a crash-after-success fault was already configured.

        Demo scenarios are configured by both the API and the worker. This guard
        keeps a one-time interruption deterministic instead of re-injecting the
        same fault on every recovery dispatch.
        """
        return bool(self._crash_configs.get((workflow_id, tool_name)))

    async def check_failure(
        self,
        workflow_id: str,
        tool_name: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Check if this tool call should fail.

        Returns the error response dict if a failure should be injected,
        or None if the tool should proceed normally.
        """
        key = (workflow_id, tool_name)
        configs = self._configs.get(key, [])
        context = context or {}

        for config in configs:
            if config.remaining_count <= 0:
                continue

            # Check if condition matches (e.g., provider=stripe)
            if config.condition:
                match = all(
                    context.get(k) == v or v == "any"
                    for k, v in config.condition.items()
                )
                if not match:
                    continue

            # Inject the failure
            config.remaining_count -= 1
            return config.error_response

        return None

    def should_crash_after_external_success(
        self,
        workflow_id: str,
        tool_name: str,
    ) -> bool:
        """Check if process should simulate a crash right after external success."""
        key = (workflow_id, tool_name)
        configs = self._crash_configs.get(key, [])
        for config in configs:
            if config.remaining_count > 0:
                config.remaining_count -= 1
                return True
        return False

    def clear(self, workflow_id: str | None = None) -> None:
        """Clear failure configs, optionally for a specific workflow."""
        if workflow_id is None:
            self._configs.clear()
            self._crash_configs.clear()
        else:
            keys_to_remove = [
                k for k in self._configs if k[0] == workflow_id
            ]
            for k in keys_to_remove:
                del self._configs[k]

            crash_keys_to_remove = [
                k for k in self._crash_configs if k[0] == workflow_id
            ]
            for k in crash_keys_to_remove:
                del self._crash_configs[k]


# ---------------------------------------------------------------------------
# Pre-built scenario configurations
# ---------------------------------------------------------------------------


def configure_scenario_1(injector: FailureInjector, workflow_id: str) -> None:
    """
    Scenario 1: Billing service unavailable.

    Stripe is down. The agent must discover this, find an alternative
    provider, and reason about which one satisfies the outcome contract.
    """
    injector.configure_failure(
        workflow_id=workflow_id,
        tool_name="setup_billing",
        failure_type="service_unavailable",
        error_response={
            "status": "error",
            "error_type": "service_unavailable",
            "message": "Billing service 'stripe' is temporarily unavailable. "
                       "HTTP 503: Service Unavailable.",
            "provider": "stripe",
        },
        remaining_count=999,  # Stripe stays down for this workflow
        condition={"provider": "stripe"},
        affects_status_check=True,
    )


def configure_scenario_2(injector: FailureInjector, workflow_id: str) -> None:
    """
    Scenario 2: Contradictory billing evidence.

    Billing tool succeeds but returns the WRONG plan tier.
    Verification will catch the discrepancy. The policy engine
    will require human approval because evidence is contradictory.
    """
    # Guard: don't re-inject if already configured (worker re-configures on each
    # Pub/Sub message including APPROVAL_RESUME — without this guard the one-shot
    # contradictory result keeps re-arming and the workflow loops forever).
    if injector.has_failure_config(workflow_id, "setup_billing"):
        return

    injector.configure_failure(
        workflow_id=workflow_id,
        tool_name="setup_billing",
        failure_type="contradictory_result",
        error_response={
            "status": "success",
            "subscription_id": "sub-contradictory",
            "customer_id": "",  # Will be filled by service
            "provider": "stripe",
            "plan_tier": "starter",      # WRONG — customer requested "enterprise"
            "billing_cycle": "monthly",
            "created_at": "2026-08-26T12:00:00Z",
        },
        remaining_count=1,
        condition={},
    )


def configure_scenario_3(injector: FailureInjector, workflow_id: str) -> None:
    """
    Scenario 3: Worker interruption.

    A worker is deterministically interrupted after the billing provider has
    accepted the idempotent mutation, but before local completion is persisted.
    RecoveryOS must reconcile authoritative external state and resume without
    creating a second subscription.
    """
    if not injector.has_crash_after_external_success_config(
        workflow_id, "setup_billing"
    ):
        injector.configure_crash_after_external_success(
            workflow_id=workflow_id,
            tool_name="setup_billing",
            remaining_count=1,
        )
