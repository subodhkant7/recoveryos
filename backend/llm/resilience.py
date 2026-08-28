"""
Runtime Gemini Resilience, Rate Limiting & Failure Recovery Subsystem.

Provides centralized request pacing, deterministic failure classification,
exponential backoff with jitter, circuit breaking, and structured observability.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncGenerator

from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from backend.config import config

logger = logging.getLogger("recoveryos.llm.resilience")


class ErrorCategory(str, Enum):
    """Classification of LLM service failure."""
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"


class CircuitState(str, Enum):
    """State machine states for the Gemini circuit breaker."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Raised when requests are blocked because the circuit breaker is OPEN."""
    pass


class RetryExhaustedError(Exception):
    """Raised when bounded retries have been exhausted."""
    pass


# ---------------------------------------------------------------------------
# Structured Observability Hooks (Redacted)
# ---------------------------------------------------------------------------

_RESILIENCE_EVENTS: list[dict[str, Any]] = []


def record_resilience_event(
    event_type: str,
    action: str = "",
    outcome: str = "",
    detail: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Record an immutable resilience event, redacting sensitive tokens/prompts/PII.
    """
    clean_extra = {
        k: v for k, v in (extra or {}).items()
        if k.lower() not in ("token", "jwt", "authorization", "secret", "key", "api_key", "prompt")
    }

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "action": action,
        "outcome": outcome,
        "detail": detail,
        "metadata": clean_extra,
    }

    _RESILIENCE_EVENTS.append(event)
    logger.info(f"[GEMINI_RESILIENCE] type={event_type} action={action} outcome={outcome} detail='{detail}'")
    return event


def get_resilience_events() -> list[dict[str, Any]]:
    """Retrieve recorded resilience events."""
    return list(_RESILIENCE_EVENTS)


def clear_resilience_events() -> None:
    """Clear recorded resilience events (for test isolation)."""
    _RESILIENCE_EVENTS.clear()


# ---------------------------------------------------------------------------
# Deterministic Failure Classifier
# ---------------------------------------------------------------------------

class GeminiFailureClassifier:
    """
    Deterministically categorizes exceptions into RETRYABLE vs NON_RETRYABLE.
    """

    @staticmethod
    def classify(exc: Exception) -> tuple[ErrorCategory, str, float | None]:
        """
        Returns (ErrorCategory, reason, optional_retry_after_seconds).
        """
        exc_str = str(exc).lower()
        exc_type = type(exc).__name__

        # 1. Check for timeout
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in exc_str:
            return ErrorCategory.RETRYABLE, "Request timeout", None

        # 2. Check for 429 / RESOURCE_EXHAUSTED / Quota
        if "429" in exc_str or "resource_exhausted" in exc_str or "quota" in exc_str or "rate limit" in exc_str:
            # Check for retry-after in exception attributes
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is None and hasattr(exc, "headers") and isinstance(exc.headers, dict):
                try:
                    retry_after = float(exc.headers.get("retry-after", 0))
                except (ValueError, TypeError):
                    retry_after = None
            return ErrorCategory.RETRYABLE, "Rate limit / quota exceeded (429)", retry_after

        # 3. Check for transient 503 / 502 / network connectivity
        if any(term in exc_str for term in ("503", "502", "service unavailable", "connection reset", "connection refused", "server disconnected")):
            return ErrorCategory.RETRYABLE, "Transient upstream service unavailable", None

        # 4. Check for Non-Retryable errors (401, 403, 400, Invalid Key, Invalid Model)
        if any(term in exc_str for term in ("401", "403", "unauthorized", "permission_denied", "api_key_invalid", "invalid_argument", "model not found", "models/")):
            return ErrorCategory.NON_RETRYABLE, "Authentication, permission, or invalid model error", None

        # Default to non-retryable for safety
        return ErrorCategory.NON_RETRYABLE, f"Unclassified error: {exc_type}", None


# ---------------------------------------------------------------------------
# Centralized Runtime Rate Limiter
# ---------------------------------------------------------------------------

class GeminiRateLimiter:
    """
    Centralized rate limiter coordinating all Gemini generation requests
    across concurrent async tasks within the process.
    """

    def __init__(self, min_interval_seconds: float | None = None):
        self._min_interval = min_interval_seconds if min_interval_seconds is not None else config.gemini_min_interval_seconds
        self._last_request_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def min_interval(self) -> float:
        return self._min_interval

    @min_interval.setter
    def min_interval(self, value: float) -> None:
        self._min_interval = max(0.0, value)

    async def acquire(self) -> float:
        """
        Acquires permission to execute a Gemini call.
        Asynchronously waits if the elapsed time since last call is < min_interval.
        Returns the duration waited in seconds.
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            wait_time = 0.0

            if self._last_request_time > 0 and elapsed < self._min_interval:
                wait_time = self._min_interval - elapsed
                record_resilience_event(
                    event_type="RATE_LIMIT_WAIT",
                    action="acquire_rate_limiter",
                    outcome="WAITING",
                    detail=f"Pacing Gemini call: waiting {wait_time:.2f}s",
                    extra={"wait_seconds": wait_time},
                )
                await asyncio.sleep(wait_time)

            self._last_request_time = time.monotonic()
            return wait_time


# ---------------------------------------------------------------------------
# Gemini Circuit Breaker
# ---------------------------------------------------------------------------

class GeminiCircuitBreaker:
    """
    Lightweight circuit breaker for protecting Gemini quota from repeated cascading failures.
    State transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED.
    """

    def __init__(
        self,
        failure_threshold: int | None = None,
        cooldown_seconds: float | None = None,
    ):
        self.failure_threshold = failure_threshold if failure_threshold is not None else config.gemini_circuit_failure_threshold
        self.cooldown_seconds = cooldown_seconds if cooldown_seconds is not None else config.gemini_circuit_cooldown_seconds
        self.state: CircuitState = CircuitState.CLOSED
        self.consecutive_failures: int = 0
        self.last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    async def can_execute(self) -> bool:
        """Check if request is permitted by circuit breaker."""
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            now = time.monotonic()
            if self.state == CircuitState.OPEN:
                if now - self.last_failure_time >= self.cooldown_seconds:
                    self.state = CircuitState.HALF_OPEN
                    record_resilience_event(
                        event_type="CIRCUIT_HALF_OPEN",
                        action="probe_circuit",
                        outcome="HALF_OPEN",
                        detail=f"Circuit cooldown elapsed ({self.cooldown_seconds}s). Transitioning to HALF_OPEN probe.",
                    )
                    return True
                return False

            if self.state == CircuitState.HALF_OPEN:
                # In half-open, allow the probe
                return True

            return True

    async def record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            prev_state = self.state
            self.consecutive_failures = 0
            self.state = CircuitState.CLOSED
            if prev_state != CircuitState.CLOSED:
                record_resilience_event(
                    event_type="CIRCUIT_CLOSED",
                    action="close_circuit",
                    outcome="CLOSED",
                    detail=f"Circuit recovered from {prev_state.value} to CLOSED.",
                )

    async def record_failure(self) -> None:
        """Record a failed call."""
        async with self._lock:
            self.consecutive_failures += 1
            self.last_failure_time = time.monotonic()

            if self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN) and self.consecutive_failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                record_resilience_event(
                    event_type="CIRCUIT_OPENED",
                    action="trip_circuit",
                    outcome="OPEN",
                    detail=f"Consecutive failures ({self.consecutive_failures}) exceeded threshold ({self.failure_threshold}). Circuit OPENED.",
                    extra={"consecutive_failures": self.consecutive_failures},
                )


# Global singleton instances
global_rate_limiter = GeminiRateLimiter()
global_circuit_breaker = GeminiCircuitBreaker()


# ---------------------------------------------------------------------------
# Resilient Gemini ADK Model
# ---------------------------------------------------------------------------

class ResilientGemini(Gemini):
    """
    Subclass of ADK Gemini that intercepts generate_content_async with:
    - Global rate limiting
    - Circuit breaker gating
    - Request timeouts
    - Bounded exponential backoff with jitter on 429/transient errors
    - Redacted audit/resilience logging
    """
    model_config = {"extra": "allow", "arbitrary_types_allowed": True}

    rate_limiter: Any = None
    circuit_breaker: Any = None
    max_retries: int = 3
    initial_backoff: float = 2.0
    max_backoff: float = 30.0
    request_timeout: float = 30.0

    def __init__(
        self,
        model: str | None = None,
        client_kwargs: dict[str, Any] | None = None,
        rate_limiter: GeminiRateLimiter | None = None,
        circuit_breaker: GeminiCircuitBreaker | None = None,
        max_retries: int | None = None,
        initial_backoff: float | None = None,
        max_backoff: float | None = None,
        request_timeout: float | None = None,
        **kwargs: Any,
    ):
        model_name = model or config.gemini_model
        if model_name in ("gemini-2.5-flash", "gemini-2.5-flash-preview", "gemini-2.0-flash", "gemini-1.5-flash"):
            model_name = "gemini-3.6-flash"
        super().__init__(model=model_name, client_kwargs=client_kwargs, **kwargs)
        object.__setattr__(self, "rate_limiter", rate_limiter or global_rate_limiter)
        object.__setattr__(self, "circuit_breaker", circuit_breaker or global_circuit_breaker)
        object.__setattr__(self, "max_retries", max_retries if max_retries is not None else config.gemini_max_retries)
        object.__setattr__(self, "initial_backoff", initial_backoff if initial_backoff is not None else config.gemini_initial_backoff_seconds)
        object.__setattr__(self, "max_backoff", max_backoff if max_backoff is not None else config.gemini_max_backoff_seconds)
        object.__setattr__(self, "request_timeout", request_timeout if request_timeout is not None else config.gemini_request_timeout_seconds)

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """
        Executes generate_content_async with full resilience protection.
        """
        attempt = 0
        while True:
            attempt += 1

            # 1. Circuit breaker gate
            if not await self.circuit_breaker.can_execute():
                record_resilience_event(
                    event_type="CIRCUIT_BLOCKED",
                    action="generate_content",
                    outcome="BLOCKED",
                    detail="Gemini request rejected because circuit breaker is OPEN",
                )
                raise CircuitOpenError("Gemini circuit breaker is OPEN; refusing calls to preserve quota")

            # 2. Global rate limiter acquisition
            await self.rate_limiter.acquire()

            record_resilience_event(
                event_type="GEMINI_REQUEST_START",
                action="generate_content",
                outcome="STARTED",
                detail=f"Turn generation attempt {attempt}/{self.max_retries + 1}",
                extra={"attempt": attempt},
            )

            try:
                # 3. Execution with timeout boundary
                llm_request.model = self.model
                response_items: list[LlmResponse] = []

                async def _call_underlying():
                    async for item in super(ResilientGemini, self).generate_content_async(llm_request, stream=stream):
                        response_items.append(item)

                await asyncio.wait_for(_call_underlying(), timeout=self.request_timeout)

                # 4. Success handling
                await self.circuit_breaker.record_success()
                record_resilience_event(
                    event_type="GEMINI_REQUEST_SUCCESS",
                    action="generate_content",
                    outcome="SUCCESS",
                    detail=f"Gemini generation succeeded on attempt {attempt} with model '{self.model}'",
                    extra={"attempt": attempt, "model": self.model},
                )

                for item in response_items:
                    yield item
                return

            except Exception as e:
                category, reason, retry_after = GeminiFailureClassifier.classify(e)
                record_resilience_event(
                    event_type="GEMINI_REQUEST_FAILED",
                    action="generate_content",
                    outcome="FAILED",
                    detail=f"Attempt {attempt} failed: {reason} ({type(e).__name__})",
                    extra={"category": category.value, "attempt": attempt, "error_type": type(e).__name__},
                )

                await self.circuit_breaker.record_failure()

                if category == ErrorCategory.NON_RETRYABLE or attempt > self.max_retries:
                    if attempt > self.max_retries:
                        record_resilience_event(
                            event_type="RETRY_EXHAUSTED",
                            action="retry_exhausted",
                            outcome="EXHAUSTED",
                            detail=f"Max retries ({self.max_retries}) exhausted",
                            extra={"attempts_made": attempt},
                        )
                        raise RetryExhaustedError(f"Gemini retries exhausted after {attempt} attempts: {e}") from e
                    raise

                # Automatic model failover on quota / 429 resource exhaustion or model deprecation / 404
                if ("RESOURCE_EXHAUSTED" in str(e) or "429" in str(e) or "quota" in str(e).lower()
                    or "404" in str(e) or "no longer available" in str(e).lower() or "NOT_FOUND" in str(e)):
                    fallback_candidates = [
                        "gemini-3.6-flash",
                        "gemini-3.5-flash",
                        "gemini-3.5-flash-lite",
                        "gemini-3.1-flash-lite",
                    ]
                    current_idx = fallback_candidates.index(self.model) if self.model in fallback_candidates else -1
                    next_model = fallback_candidates[(current_idx + 1) % len(fallback_candidates)]
                    if next_model != self.model:
                        record_resilience_event(
                            event_type="MODEL_FAILOVER",
                            action="switch_model",
                            outcome="SWITCHED",
                            detail=f"Switching Gemini model from '{self.model}' to '{next_model}' due to model availability / quota limits",
                            extra={"previous_model": self.model, "new_model": next_model},
                        )
                        object.__setattr__(self, "model", next_model)
                        llm_request.model = next_model
                        delay = 0.5

                # Calculate exponential backoff with jitter
                base_backoff = min(self.max_backoff, self.initial_backoff * (2 ** (attempt - 1)))
                jitter = 0.8 + 0.4 * random.random()
                delay = base_backoff * jitter if 'delay' not in locals() else delay
                if retry_after is not None:
                    delay = max(delay, retry_after)

                record_resilience_event(
                    event_type="RETRY_SCHEDULED",
                    action="schedule_retry",
                    outcome="RETRYING",
                    detail=f"Retrying in {delay:.2f}s with model '{self.model}' (attempt {attempt}/{self.max_retries})",
                    extra={"delay_seconds": delay, "attempt": attempt, "model": self.model},
                )
                await asyncio.sleep(delay)
