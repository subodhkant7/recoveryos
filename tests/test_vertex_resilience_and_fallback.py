"""
Test suite covering Vertex AI primary model configuration,
Gemini 3.5 Flash Lite fallback mechanics, single fallback limit,
and resilience layer compatibility.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from backend.config import Config
from backend.llm.resilience import (
    CircuitState,
    GeminiCircuitBreaker,
    GeminiRateLimiter,
    ResilientGemini,
    RetryExhaustedError,
    clear_resilience_events,
    get_resilience_events,
    global_circuit_breaker,
)


@pytest.fixture(autouse=True)
def clean_events():
    clear_resilience_events()
    global_circuit_breaker.state = CircuitState.CLOSED
    global_circuit_breaker.consecutive_failures = 0
    yield
    clear_resilience_events()
    global_circuit_breaker.state = CircuitState.CLOSED
    global_circuit_breaker.consecutive_failures = 0


@pytest.mark.asyncio
async def test_01_vertex_configuration_fields():
    """Verify Config dataclass supports llm_provider, gemini_fallback_model, and vertex_location."""
    cfg = Config()
    assert hasattr(cfg, "llm_provider")
    assert hasattr(cfg, "gemini_model")
    assert hasattr(cfg, "gemini_fallback_model")
    assert hasattr(cfg, "vertex_location")
    assert cfg.gemini_model == "gemini-3.5-flash"
    assert cfg.gemini_fallback_model == "gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_02_resilient_gemini_vertex_initialization():
    """Verify ResilientGemini initializes cached client for vertex provider without requiring api_key."""
    with patch("backend.llm.resilience.config", Config(llm_provider="vertex", google_cloud_project="recoveryos-506713")):
        with patch("google.auth.default", return_value=(MagicMock(), "recoveryos-506713")):
            with patch("google.genai.Client") as mock_client_cls:
                mock_instance = MagicMock()
                mock_client_cls.return_value = mock_instance
                model = ResilientGemini(model="gemini-3.5-flash", fallback_model="gemini-3.5-flash-lite")
                assert model.model == "gemini-3.5-flash"
                assert model.fallback_model == "gemini-3.5-flash-lite"
                assert model.api_client is mock_instance


@pytest.mark.asyncio
async def test_03_primary_model_success_no_fallback():
    """Verify primary model success does not trigger fallback."""
    cb = GeminiCircuitBreaker(failure_threshold=5)
    limiter = GeminiRateLimiter(min_interval_seconds=0.0)
    model = ResilientGemini(
        model="gemini-3.5-flash",
        fallback_model="gemini-3.5-flash-lite",
        circuit_breaker=cb,
        rate_limiter=limiter,
        max_retries=2,
    )

    dummy_response = LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text="Primary model response")])
    )

    async def mock_gen(req, stream=False):
        yield dummy_response

    with patch("google.adk.models.Gemini.generate_content_async", side_effect=mock_gen):
        req = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="Hello")])],
        )
        responses = [r async for r in model.generate_content_async(req)]
        assert len(responses) == 1
        assert responses[0].content.parts[0].text == "Primary model response"

        events = get_resilience_events()
        event_types = [e["event_type"] for e in events]
        assert "GEMINI_REQUEST_START" in event_types
        assert "GEMINI_REQUEST_SUCCESS" in event_types
        assert "MODEL_FALLBACK" not in event_types


@pytest.mark.asyncio
async def test_04_fallback_to_lite_on_retryable_primary_failure():
    """Verify primary failure triggers single fallback to gemini-3.5-flash-lite."""
    cb = GeminiCircuitBreaker(failure_threshold=5)
    limiter = GeminiRateLimiter(min_interval_seconds=0.0)
    model = ResilientGemini(
        model="gemini-3.5-flash",
        fallback_model="gemini-3.5-flash-lite",
        circuit_breaker=cb,
        rate_limiter=limiter,
        max_retries=1,
        initial_backoff=0.01,
    )

    call_count = {"primary": 0, "fallback": 0}

    async def mock_gen(req, stream=False):
        if req.model == "gemini-3.5-flash":
            call_count["primary"] += 1
            if False:
                yield None
            raise Exception("429 RESOURCE_EXHAUSTED: Rate limit reached")
        elif req.model == "gemini-3.5-flash-lite":
            call_count["fallback"] += 1
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text="Fallback lite success")])
            )

    with patch("google.adk.models.Gemini.generate_content_async", side_effect=mock_gen):
        req = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="Hello")])],
        )
        responses = [r async for r in model.generate_content_async(req)]
        assert len(responses) == 1
        assert responses[0].content.parts[0].text == "Fallback lite success"

        # Primary tried initial + 1 retry = 2 attempts, then exactly 1 fallback attempt
        assert call_count["primary"] == 2
        assert call_count["fallback"] == 1

        events = get_resilience_events()
        event_types = [e["event_type"] for e in events]
        assert "MODEL_FALLBACK" in event_types
        fallback_ev = next(e for e in events if e["event_type"] == "MODEL_FALLBACK")
        assert fallback_ev["metadata"]["from_model"] == "gemini-3.5-flash"
        assert fallback_ev["metadata"]["to_model"] == "gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_05_no_fallback_on_non_retryable_error():
    """Verify non-retryable errors (e.g. 400 Bad Request) do not trigger fallback."""
    cb = GeminiCircuitBreaker(failure_threshold=5)
    limiter = GeminiRateLimiter(min_interval_seconds=0.0)
    model = ResilientGemini(
        model="gemini-3.5-flash",
        fallback_model="gemini-3.5-flash-lite",
        circuit_breaker=cb,
        rate_limiter=limiter,
        max_retries=1,
    )

    async def mock_gen(req, stream=False):
        if False:
            yield None
        raise Exception("400 Bad Request: Invalid parameter")

    with patch("google.adk.models.Gemini.generate_content_async", side_effect=mock_gen):
        req = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="Invalid")])],
        )
        with pytest.raises(Exception, match="400 Bad Request"):
            _ = [r async for r in model.generate_content_async(req)]

        events = get_resilience_events()
        event_types = [e["event_type"] for e in events]
        assert "MODEL_FALLBACK" not in event_types


@pytest.mark.asyncio
async def test_06_single_fallback_limit_and_no_infinite_loop():
    """Verify if both primary and fallback fail, error is raised and no infinite loop occurs."""
    cb = GeminiCircuitBreaker(failure_threshold=5)
    limiter = GeminiRateLimiter(min_interval_seconds=0.0)
    model = ResilientGemini(
        model="gemini-3.5-flash",
        fallback_model="gemini-3.5-flash-lite",
        circuit_breaker=cb,
        rate_limiter=limiter,
        max_retries=1,
        initial_backoff=0.01,
    )

    call_count = {"primary": 0, "fallback": 0}

    async def mock_gen(req, stream=False):
        if req.model == "gemini-3.5-flash":
            call_count["primary"] += 1
            if False:
                yield None
            raise Exception("429 RESOURCE_EXHAUSTED: Primary 429")
        elif req.model == "gemini-3.5-flash-lite":
            call_count["fallback"] += 1
            if False:
                yield None
            raise Exception("503 Service Unavailable: Fallback 503")

    with patch("google.adk.models.Gemini.generate_content_async", side_effect=mock_gen):
        req = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="Fail both")])],
        )
        with pytest.raises(RetryExhaustedError):
            _ = [r async for r in model.generate_content_async(req)]

        # Exactly 2 primary attempts + exactly 1 fallback attempt
        assert call_count["primary"] == 2
        assert call_count["fallback"] == 1

        events = get_resilience_events()
        event_types = [e["event_type"] for e in events]
        assert "MODEL_FALLBACK" in event_types
        assert "FALLBACK_FAILED" in event_types
        assert "RETRY_EXHAUSTED" in event_types
