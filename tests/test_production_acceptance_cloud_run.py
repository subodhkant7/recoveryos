"""
Phase 6.1.1: Production Acceptance Test Suite for Live Cloud Run & Firestore.

Executes comprehensive live end-to-end verification against the deployed Google Cloud Run
microservice (https://recoveryos-321161003794.asia-east1.run.app) backed by live GCP Firestore
and Secret Manager.
"""

import os
import shutil
import subprocess
import time
from datetime import timedelta
import httpx
import pytest

from backend.security.tokens import create_access_token
from backend.security.principal import Role


CLOUD_RUN_URL = "https://recoveryos-321161003794.asia-east1.run.app"
GCP_PROJECT = "recoveryos-506713"


def _get_gcloud_bin() -> str:
    return shutil.which("gcloud") or "gcloud"


@pytest.fixture(scope="session")
def gcp_identity_token():
    """Obtain valid GCP Identity Token to pass Cloud Run edge IAM."""
    cmd = [_get_gcloud_bin(), "auth", "print-identity-token"]
    try:
        token = subprocess.check_output(cmd).decode().strip()
        return token
    except Exception as e:
        pytest.skip(f"GCP Identity Token unavailable: {e}")


@pytest.fixture(scope="session")
def jwt_secret():
    """Retrieve the production JWT secret key from Secret Manager."""
    cmd = [
        _get_gcloud_bin(),
        "secrets", "versions", "access", "latest",
        f"--secret=recoveryos-jwt-secret",
        f"--project={GCP_PROJECT}",
    ]
    try:
        secret = subprocess.check_output(cmd).decode().strip()
        return secret
    except Exception as e:
        pytest.skip(f"Secret Manager JWT Secret unavailable: {e}")


@pytest.fixture
def api_client():
    """HTTP client configured with 30s timeout and certifi trust."""
    with httpx.Client(base_url=CLOUD_RUN_URL, timeout=30.0) as client:
        yield client


# ===========================================================================
# 1. Cloud Run Edge IAM Authentication Tests
# ===========================================================================

def test_prod_01_public_judge_access_to_health(api_client):
    """Phase 40: Public judge demonstration service is accessible without private IAM authentication."""
    try:
        response = api_client.get("/api/health")
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
        pytest.skip(f"Cloud Run endpoint unreachable (offline / no network): {e}")
    assert response.status_code == 200, f"Expected 200 OK for judge access, got {response.status_code}"
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "recoveryos"


def test_prod_02_authenticated_iam_probe_reaches_backend(api_client, gcp_identity_token):
    """Authenticated probe passes edge proxy and reaches /api/health."""
    headers = {"X-Serverless-Authorization": f"Bearer {gcp_identity_token}"}
    response = api_client.get("/api/health", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "recoveryos"
    assert data["environment"] == "production"
    assert data["model"] == "gemini-3.5-flash"


def test_prod_03_readiness_probe_verifies_live_firestore(api_client, gcp_identity_token):
    """Readiness probe verifies live GCP Firestore backend is active and ready."""
    headers = {"X-Serverless-Authorization": f"Bearer {gcp_identity_token}"}
    response = api_client.get("/api/ready", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["persistence_backend"] == "firestore"


# ===========================================================================
# 2. Application JWT Authentication Tests
# ===========================================================================

def test_prod_04_missing_application_jwt_rejected(api_client, gcp_identity_token):
    """Protected API endpoint returns 401 when application JWT is missing."""
    headers = {"X-Serverless-Authorization": f"Bearer {gcp_identity_token}"}
    response = api_client.get("/api/workflows", headers=headers)
    assert response.status_code == 401
    assert "detail" in response.json()


def test_prod_05_invalid_signature_jwt_rejected(api_client, gcp_identity_token):
    """JWT signed with incorrect secret is rejected with 401 Unauthorized."""
    fake_token = create_access_token("attacker", Role.ADMIN, secret_key="wrong-secret-key-32bytes-long-padding")
    headers = {
        "X-Serverless-Authorization": f"Bearer {gcp_identity_token}",
        "Authorization": f"Bearer {fake_token}",
    }
    response = api_client.get("/api/workflows", headers=headers)
    assert response.status_code == 401


def test_prod_06_expired_jwt_rejected(api_client, gcp_identity_token, jwt_secret):
    """Expired application JWT is rejected with 401 Unauthorized."""
    expired_token = create_access_token(
        "user-expired", Role.OPERATOR, expires_delta=timedelta(seconds=-60), secret_key=jwt_secret
    )
    headers = {
        "X-Serverless-Authorization": f"Bearer {gcp_identity_token}",
        "Authorization": f"Bearer {expired_token}",
    }
    response = api_client.get("/api/workflows", headers=headers)
    assert response.status_code == 401


# ===========================================================================
# 3. RBAC & Privilege Gating Tests
# ===========================================================================

def test_prod_07_viewer_cannot_launch_scenario(api_client, gcp_identity_token, jwt_secret):
    """User with VIEWER role cannot launch workflow scenarios (403 Forbidden)."""
    viewer_token = create_access_token("viewer-1", Role.VIEWER, tenant_id="tenant-prod-test", secret_key=jwt_secret)
    headers = {
        "X-Serverless-Authorization": f"Bearer {gcp_identity_token}",
        "Authorization": f"Bearer {viewer_token}",
    }
    response = api_client.post("/api/scenarios/billing_unavailable", headers=headers)
    assert response.status_code == 403


def test_prod_08_operator_can_launch_scenario(api_client, gcp_identity_token, jwt_secret):
    """User with OPERATOR role can launch workflow scenarios."""
    operator_token = create_access_token("operator-1", Role.OPERATOR, tenant_id="tenant-prod-test", secret_key=jwt_secret)
    headers = {
        "X-Serverless-Authorization": f"Bearer {gcp_identity_token}",
        "Authorization": f"Bearer {operator_token}",
    }
    response = api_client.post("/api/scenarios/billing_unavailable", headers=headers)
    assert response.status_code in (200, 202)
    data = response.json()
    assert data["status"] in ("launched", "dispatched")
    assert "workflow_id" in data
    assert data["tenant_id"] == "tenant-prod-test"


def test_prod_09_operator_cannot_approve_workflow(api_client, gcp_identity_token, jwt_secret):
    """User with OPERATOR role cannot call human approval endpoints (403 Forbidden)."""
    operator_token = create_access_token("operator-1", Role.OPERATOR, tenant_id="tenant-prod-test", secret_key=jwt_secret)
    headers = {
        "X-Serverless-Authorization": f"Bearer {gcp_identity_token}",
        "Authorization": f"Bearer {operator_token}",
    }
    dummy_wf_id = "wf-dummy-123"
    response = api_client.post(
        f"/api/workflows/{dummy_wf_id}/approve/appr-123",
        headers=headers,
        json={"approved": True, "reason": "Test approve"},
    )
    assert response.status_code == 403


# ===========================================================================
# 4. Tenant Isolation Tests
# ===========================================================================

def test_prod_10_cross_tenant_workflow_read_rejected(api_client, gcp_identity_token, jwt_secret):
    """Tenant B operator cannot read or access Tenant A's workflow."""
    # 1. Tenant A launches workflow
    token_a = create_access_token("op-a", Role.OPERATOR, tenant_id="tenant-prod-alpha", secret_key=jwt_secret)
    headers_a = {
        "X-Serverless-Authorization": f"Bearer {gcp_identity_token}",
        "Authorization": f"Bearer {token_a}",
    }
    res_a = api_client.post("/api/scenarios/billing_unavailable", headers=headers_a)
    assert res_a.status_code in (200, 202)
    wf_id = res_a.json()["workflow_id"]

    # 2. Tenant B attempts to read Tenant A's workflow
    token_b = create_access_token("op-b", Role.OPERATOR, tenant_id="tenant-prod-beta", secret_key=jwt_secret)
    headers_b = {
        "X-Serverless-Authorization": f"Bearer {gcp_identity_token}",
        "Authorization": f"Bearer {token_b}",
    }
    res_b = api_client.get(f"/api/workflows/{wf_id}", headers=headers_b)
    assert res_b.status_code == 403
    assert "Cross-tenant access forbidden" in res_b.json()["detail"]


# ===========================================================================
# 5. Live Firestore Workflow Lifecycle & Gemini Execution
# ===========================================================================

def test_prod_11_live_workflow_execution_and_firestore_persistence(api_client, gcp_identity_token, jwt_secret):
    """
    Launch a scenario on Cloud Run, verify Firestore persistence, correlation headers,
    and background autonomous execution.
    """
    tenant_id = "tenant-prod-exec"
    op_token = create_access_token("operator-exec", Role.OPERATOR, tenant_id=tenant_id, secret_key=jwt_secret)
    headers = {
        "X-Serverless-Authorization": f"Bearer {gcp_identity_token}",
        "Authorization": f"Bearer {op_token}",
        "X-Request-ID": "req-smoke-test-001",
    }

    # 1. Launch scenario
    launch_res = api_client.post("/api/scenarios/billing_unavailable", headers=headers)
    assert launch_res.status_code in (200, 202)
    # Correlation header reflection
    assert launch_res.headers.get("x-request-id") == "req-smoke-test-001"
    wf_id = launch_res.json()["workflow_id"]

    # 2. Query workflow snapshot from live Firestore
    time.sleep(2.0)
    wf_res = api_client.get(f"/api/workflows/{wf_id}", headers=headers)
    assert wf_res.status_code == 200
    wf_data = wf_res.json()
    assert wf_data["workflow"]["workflow_id"] == wf_id
    assert wf_data["workflow"]["tenant_id"] == tenant_id
    assert "events" in wf_data
    assert "steps" in wf_data
    assert "evidence" in wf_data


# ===========================================================================
# 6. Observability, Prometheus Metrics & Cloud Logging
# ===========================================================================

def test_prod_12_prometheus_metrics_export(api_client, gcp_identity_token):
    """GET /metrics exports valid Prometheus format with low-cardinality labels."""
    headers = {"X-Serverless-Authorization": f"Bearer {gcp_identity_token}"}
    res = api_client.get("/metrics", headers=headers)
    assert res.status_code == 200
    text = res.text
    assert "# TYPE recoveryos_http_requests_total counter" in text
    assert "# TYPE recoveryos_http_request_duration_seconds histogram" in text


def test_prod_13_cloud_logging_sanitization(jwt_secret):
    """Verify that Cloud Logging logs do not contain leaked secrets, JWTs, or API keys."""
    cmd = [
        _get_gcloud_bin(),
        "logging", "read",
        f"resource.type=cloud_run_revision AND resource.labels.service_name=recoveryos AND resource.labels.revision_name=recoveryos-00004-sw7",
        "--limit=50",
        f"--project={GCP_PROJECT}",
        "--format=json",
    ]
    try:
        output = subprocess.check_output(cmd).decode()
        # Secret string must never appear in raw logs
        assert jwt_secret not in output, "JWT secret found in Cloud Run logs!"
        assert "AIzaSy" not in output, "Google API key pattern found in Cloud Run logs!"
        assert "Bearer eyJ" not in output, "Raw Authorization Bearer token found in Cloud Run logs!"
    except subprocess.CalledProcessError as e:
        pytest.skip(f"Could not read Cloud Logging: {e}")
