"""
Phase 28/29: RecoveryOS Hackathon Judge-Ready Demo Hardening & Final Polish Tests.

Tests:
1. Root and console route HTML structure and presentation hooks.
2. 4-Questions Decision Inspector elements presence (01-04).
3. Action != Recovery principle callout and Recovery Proof Certificate elements.
4. Demo mode and Live/Replay mode separation.
5. Scenario launcher modal with 3 distinct real scenarios and capability CTAs.
6. Autonomy decision card and worker resilience banner.
7. Security checks (no secrets exposed in DOM or static files).
8. Preservation of backward-compatible identifiers.
"""

import os
import pytest
import httpx
from backend.api.server import app
from backend.security.tokens import create_access_token
from backend.security.principal import Role


def _make_auth_headers(user_id="operator", role=Role.OPERATOR, tenant_id="tenant-default"):
    token = create_access_token(user_id=user_id, role=role, tenant_id=tenant_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_judge_demo_01_root_and_console_html():
    """Verify root / and /console routes serve presentation-grade Judge Demo HTML."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "RECOVERY<span class=\"brand-glow\">OS</span>" in resp.text
        assert "AUTONOMOUS OPERATIONS COMMAND CENTER" in resp.text
        assert "DEMO MODE • LIVE BACKEND" in resp.text
        assert "ACTION EXECUTED ≠ RECOVERY VERIFIED" in resp.text
        assert "RECOVERY PROOF" in resp.text
        assert "AUTONOMY DECISION" in resp.text
        assert "WHY RECOVERYOS?" in resp.text


@pytest.mark.asyncio
async def test_judge_demo_02_four_questions_inspector():
    """Verify the 4 Core Decision Audit Questions (01-04) exist in index.html."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/console/index.html")
        assert resp.status_code == 200
        assert "01" in resp.text and "WHAT DID YOU SEE?" in resp.text
        assert "02" in resp.text and "WHAT DID YOU THINK?" in resp.text
        assert "03" in resp.text and "WHAT DID YOU DO?" in resp.text
        assert "04" in resp.text and "HOW DO YOU KNOW IT WORKED?" in resp.text
        assert "AUTONOMOUS CONFIDENCE" in resp.text


@pytest.mark.asyncio
async def test_judge_demo_03_autonomy_boundary_and_scenarios():
    """Verify scenario launcher modal has all 3 real scenarios, dynamic CTAs, and autonomy boundary."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/console/index.html")
        assert resp.status_code == 200
        assert "SCENARIO 01 • AUTONOMOUS RECOVERY" in resp.text
        assert "SCENARIO 02 • BOUNDED AUTONOMY" in resp.text
        assert "SCENARIO 03 • RESILIENT EXECUTION" in resp.text
        assert "AUTONOMY BOUNDARY REACHED: HUMAN APPROVAL REQUIRED" in resp.text
        assert "WORKER INTERRUPTION &amp; LEASE RECONCILIATION" in resp.text
        assert "WHY WE STOPPED:" in resp.text


@pytest.mark.asyncio
async def test_judge_demo_04_static_assets_and_compatibility():
    """Verify CSS, JS, and backward-compatible test hooks."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Check styles.css
        resp_css = await client.get("/console/styles.css")
        assert resp_css.status_code == 200
        assert "--cyan-core" in resp_css.text
        assert "--bg-app" in resp_css.text
        assert "prefers-reduced-motion" in resp_css.text
        assert "autonomy-decision-card" in resp_css.text
        assert "worker-resilience-card" in resp_css.text

        # Check app.js
        resp_js = await client.get("/console/app.js")
        assert resp_js.status_code == 200
        assert "connectWorkflowStream" in resp_js.text
        assert "loadFleetOverview" in resp_js.text
        assert "showRecoveryProof" in resp_js.text
        assert "updateStoryLifecycle" in resp_js.text
        assert "toggleDemoMode" in resp_js.text
        assert "updateAutonomyDecisionCard" in resp_js.text


@pytest.mark.asyncio
async def test_judge_demo_05_security_no_secret_exposure():
    """Verify no secret tokens or keys are present in static assets."""
    static_dir = os.path.join(os.path.dirname(__file__), "..", "backend", "api", "static")
    for filename in ["index.html", "styles.css", "app.js"]:
        path = os.path.join(static_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "AIzaSy" not in content  # No GCP API keys
            assert "recoveryos-jwt-secret-key" not in content  # No raw JWT secret
            assert "Bearer ya29." not in content  # No OAuth access tokens
