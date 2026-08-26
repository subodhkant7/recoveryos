"""
LLM Resilience & Gateway package.
"""

from backend.llm.resilience import (
    ErrorCategory,
    CircuitState,
    CircuitOpenError,
    RetryExhaustedError,
    GeminiFailureClassifier,
    GeminiRateLimiter,
    GeminiCircuitBreaker,
    ResilientGemini,
    global_rate_limiter,
    global_circuit_breaker,
    record_resilience_event,
    get_resilience_events,
    clear_resilience_events,
)

__all__ = [
    "ErrorCategory",
    "CircuitState",
    "CircuitOpenError",
    "RetryExhaustedError",
    "GeminiFailureClassifier",
    "GeminiRateLimiter",
    "GeminiCircuitBreaker",
    "ResilientGemini",
    "global_rate_limiter",
    "global_circuit_breaker",
    "record_resilience_event",
    "get_resilience_events",
    "clear_resilience_events",
]
