"""
Phase 5.4.5: Health and Readiness Endpoints Test Suite.

Verifies liveness (/api/health) and backend readiness (/api/ready) behavior.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from backend.api.server import app
from backend.config import config


@pytest.mark.asyncio
async def test_health_liveness_endpoint():
    """Verify /api/health returns healthy liveness status."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "recoveryos"
        assert "timestamp" in data


@pytest.mark.asyncio
async def test_readiness_in_memory_backend():
    """Verify /api/ready succeeds when in_memory backend is configured."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["persistence_backend"] == "in_memory"


@pytest.mark.asyncio
async def test_readiness_firestore_unconfigured_fails():
    """Verify /api/ready returns 503 when Firestore is configured but uninitialized."""
    from backend.config import Config
    custom_cfg = Config(
        persistence_backend="firestore",
        firestore_emulator_host="",
        google_cloud_project="",
    )
    with patch("backend.api.server.config", custom_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/ready")
            assert resp.status_code == 503
            assert "unconfigured" in resp.text
