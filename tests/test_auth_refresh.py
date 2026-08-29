import asyncio
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.server import app
from backend.config import config
from backend.security.principal import Role
from backend.security.tokens import create_access_token, create_refresh_token, verify_access_token


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_login_issues_refresh_token_and_refreshes_access_token(client):
    async with client as ac:
        login = await ac.post("/api/auth/login", json={"username": "operator"})
        assert login.status_code == 200
        data = login.json()
        assert data["refresh_token"]
        refreshed = await ac.post("/api/auth/refresh", json={"refresh_token": data["refresh_token"]})
        assert refreshed.status_code == 200
        assert verify_access_token(refreshed.json()["access_token"]).user_id == "operator"


@pytest.mark.asyncio
async def test_expired_access_token_can_be_replaced_before_original_request(client):
    refresh = create_refresh_token("operator", Role.OPERATOR, secret_key=config.jwt_secret_key)
    expired = create_access_token(
        "operator", Role.OPERATOR, expires_delta=timedelta(seconds=-10), secret_key=config.jwt_secret_key
    )
    async with client as ac:
        rejected = await ac.get("/api/workflows", headers={"Authorization": f"Bearer {expired}"})
        assert rejected.status_code == 401
        renewed = await ac.post("/api/auth/refresh", json={"refresh_token": refresh})
        assert renewed.status_code == 200
        retried = await ac.get(
            "/api/workflows",
            headers={"Authorization": f"Bearer {renewed.json()['access_token']}"},
        )
        assert retried.status_code == 200


@pytest.mark.asyncio
async def test_invalid_refresh_token_is_rejected_without_access_bypass(client):
    async with client as ac:
        response = await ac.post("/api/auth/refresh", json={"refresh_token": "not-a-token"})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_concurrent_refresh_requests_are_all_valid_and_refresh_does_not_change_identity(client):
    refresh = create_refresh_token("operator", Role.OPERATOR, secret_key=config.jwt_secret_key)
    async with client as ac:
        responses = await asyncio.gather(*[
            ac.post("/api/auth/refresh", json={"refresh_token": refresh})
            for _ in range(3)
        ])
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["role"] == "operator" for response in responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["billing_unavailable", "contradictory_evidence", "worker_interruption"])
async def test_scenario_launch_succeeds_after_access_token_renewal(client, scenario):
    refresh = create_refresh_token("operator", Role.OPERATOR, secret_key=config.jwt_secret_key)
    async with client as ac:
        renewed = await ac.post("/api/auth/refresh", json={"refresh_token": refresh})
        response = await ac.post(
            f"/api/scenarios/{scenario}",
            headers={"Authorization": f"Bearer {renewed.json()['access_token']}"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "launched"


@pytest.mark.asyncio
async def test_sse_ticket_can_be_issued_after_access_token_renewal(client):
    refresh = create_refresh_token("operator", Role.OPERATOR, secret_key=config.jwt_secret_key)
    async with client as ac:
        launched = await ac.post("/api/scenarios/billing_unavailable", headers={
            "Authorization": f"Bearer {(await ac.post('/api/auth/refresh', json={'refresh_token': refresh})).json()['access_token']}"
        })
        workflow_id = launched.json()["workflow_id"]
        renewed = await ac.post("/api/auth/refresh", json={"refresh_token": refresh})
        ticket = await ac.post(
            "/api/auth/sse-ticket",
            json={"workflow_id": workflow_id},
            headers={"Authorization": f"Bearer {renewed.json()['access_token']}"},
        )
    assert ticket.status_code == 200
    assert ticket.json()["ticket"].startswith("sset_")
