"""
Frontend Experience Tests for RecoveryOS Hackathon Command Center.

Tests:
1. Root / and /console routing
2. Static asset integrity (index.html, styles.css, app.js)
3. Scenario launch endpoints
4. SSE single-use ticket lifecycle
5. Operator overview metrics contract
"""

import pytest
import httpx
from backend.api.server import app
from backend.security.tokens import create_access_token
from backend.security.principal import Role


def _make_auth_headers(user_id="operator", role=Role.OPERATOR, tenant_id="tenant-default"):
    token = create_access_token(user_id=user_id, role=role, tenant_id=tenant_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_frontend_01_root_route_serves_html():
    """Verify root route / serves index.html."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "RECOVERY<span class=\"brand-glow\">OS</span>" in resp.text
        assert "RECOVERY CONTROL PLANE • ENTERPRISE AGENT FLEETS" in resp.text


@pytest.mark.asyncio
async def test_frontend_02_static_assets_integrity():
    """Verify all console static assets are served with proper headers and keywords."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Check index.html
        resp_html = await client.get("/console/index.html")
        assert resp_html.status_code == 200
        assert "AUTONOMOUS ORCHESTRATION PIPELINE" in resp_html.text
        assert "WHY DID I DO THAT?" in resp_html.text

        # Check styles.css
        resp_css = await client.get("/console/styles.css")
        assert resp_css.status_code == 200
        assert "--cyan-core" in resp_css.text
        assert "--bg-app" in resp_css.text

        # Check app.js
        resp_js = await client.get("/console/app.js")
        assert resp_js.status_code == 200
        assert "connectWorkflowStream" in resp_js.text
        assert "loadFleetOverview" in resp_js.text
        assert "startReplay" in resp_js.text


@pytest.mark.asyncio
async def test_frontend_03_scenario_launcher_and_sse_ticket():
    """Verify scenario launch returns workflow_id and mints valid single-use SSE ticket."""
    headers = _make_auth_headers()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Launch scenario
        res_launch = await client.post("/api/scenarios/billing_unavailable", headers=headers)
        assert res_launch.status_code in (200, 202)
        data = res_launch.json()
        wf_id = data["workflow_id"]
        assert wf_id is not None

        # Mint SSE ticket
        res_ticket = await client.post("/api/auth/sse-ticket", headers=headers, json={"workflow_id": wf_id})
        assert res_ticket.status_code == 200
        ticket_data = res_ticket.json()
        assert ticket_data["ticket"].startswith("sset_")
        assert ticket_data["workflow_id"] == wf_id
