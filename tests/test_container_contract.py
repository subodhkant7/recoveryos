"""
Phase 5.4.5: Docker Container Contract Test Suite.

Verifies Dockerfile syntax, non-root user declaration, healthcheck, and compose configuration.
"""

from pathlib import Path


def test_dockerfile_contract():
    """Verify Dockerfile enforces non-root user, Python unbuffering, and HEALTHCHECK."""
    dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
    assert dockerfile_path.exists(), "Dockerfile must exist"

    content = dockerfile_path.read_text()

    # Must use non-root user
    assert "useradd" in content or "adduser" in content
    assert "USER appuser" in content or "USER 10001" in content

    # Must configure unbuffered Python output
    assert "PYTHONUNBUFFERED=1" in content

    # Must specify HEALTHCHECK
    assert "HEALTHCHECK" in content
    assert "/api/health" in content

    # Must expose port 8000
    assert "EXPOSE 8000" in content

    # Must not contain hardcoded secrets
    assert "AIza" not in content
    assert "eyJh" not in content


def test_docker_compose_contract():
    """Verify docker-compose.yml defines recoveryos-api with proper port and environment."""
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml must exist"

    content = compose_path.read_text()
    assert "recoveryos-api" in content
    assert "8000:8000" in content
    assert "ENVIRONMENT=production" in content
