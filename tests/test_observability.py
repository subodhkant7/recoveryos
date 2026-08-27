"""
Phase 5.4.5: Structured Logging, Secret Redaction & Correlation ID Test Suite.

Verifies:
- Machine-readable JSON log output.
- Recursive redaction of JWTs, API keys, passwords, and secrets.
- Correlation/request ID propagation across context and response headers.
- Prometheus metrics counting and latency observation.
"""

import json
import logging
import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.server import app
from backend.observability.logging import (
    StructuredJsonFormatter,
    redact_sensitive_data,
    current_request_id,
    current_workflow_id,
    current_tenant_id,
)
from backend.observability.metrics import metrics
from backend.security.tokens import create_access_token
from backend.security.principal import Role


@pytest.fixture(autouse=True)
def reset_observability():
    metrics.clear()
    current_request_id.set("")
    current_workflow_id.set("")
    current_tenant_id.set("")
    yield
    current_request_id.set("")
    current_workflow_id.set("")
    current_tenant_id.set("")


def test_structured_json_formatting():
    """Verify log record is converted to valid JSON with required fields."""
    formatter = StructuredJsonFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Workflow execution step completed",
        args=(),
        exc_info=None,
    )

    current_request_id.set("req-test-12345")
    current_workflow_id.set("wf-test-67890")
    current_tenant_id.set("tenant-acme")

    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert parsed["level"] == "INFO"
    assert parsed["service"] == "recoveryos"
    assert parsed["message"] == "Workflow execution step completed"
    assert parsed["request_id"] == "req-test-12345"
    assert parsed["workflow_id"] == "wf-test-67890"
    assert parsed["tenant_id"] == "tenant-acme"
    assert "timestamp" in parsed


def test_recursive_secret_redaction():
    """Verify recursive dictionary/list redaction masks sensitive keys and embedded tokens."""
    payload = {
        "user": "alice",
        "api_key": "AIzaSyD-SecretApiKey12345678901234567",
        "nested": {
            "password": "SuperSecretPassword123!",
            "token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgN_bE_dummy_sig",
            "normal_field": "visible_data",
            "items": [
                {"secret_key": "hidden_secret", "label": "safe_label"},
                "Inline key: AIzaSyD-SecretApiKey12345678901234567",
            ],
        },
    }

    redacted = redact_sensitive_data(payload)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["nested"]["normal_field"] == "visible_data"
    assert redacted["nested"]["items"][0]["secret_key"] == "[REDACTED]"
    assert redacted["nested"]["items"][0]["label"] == "safe_label"
    assert "[REDACTED_API_KEY]" in redacted["nested"]["items"][1]


@pytest.mark.asyncio
async def test_correlation_id_middleware_and_header():
    """Verify request ID is generated/propagated and returned in response headers."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Case 1: Custom safe X-Request-ID supplied
        resp1 = await client.get("/api/health", headers={"X-Request-ID": "custom-req-001"})
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Request-ID") == "custom-req-001"

        # Case 2: Auto-generated X-Request-ID
        resp2 = await client.get("/api/health")
        assert resp2.status_code == 200
        auto_id = resp2.headers.get("X-Request-ID")
        assert auto_id is not None
        assert auto_id.startswith("req-")


@pytest.mark.asyncio
async def test_prometheus_metrics_increment_and_export():
    """Verify HTTP requests increment Prometheus metrics and export in /metrics."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Generate some traffic
        await client.get("/api/health")
        await client.get("/api/health")

        # Fetch metrics
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        content = resp.text

        assert "recoveryos_http_requests_total" in content
        assert 'endpoint="/api/health"' in content
        assert 'status="200"' in content
        assert "recoveryos_http_request_duration_seconds" in content
