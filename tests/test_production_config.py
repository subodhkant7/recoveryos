"""
Phase 5.4.5: Production Configuration Hardening Test Suite.

Verifies fail-closed configuration validation rules for production deployments.
"""

import pytest
from backend.config import Config


def test_production_config_rejects_default_jwt_secret():
    """Verify production mode rejects default or weak JWT secrets."""
    cfg = Config(
        environment="production",
        jwt_secret_key="recoveryos-prod-default-secret-change-in-env-32bytes",
        cors_allow_origins=["https://dashboard.recoveryos.com"],
        persistence_backend="in_memory",
    )
    with pytest.raises(ValueError) as exc_info:
        cfg.validate_production_config()
    assert "JWT_SECRET_KEY" in str(exc_info.value)


def test_production_config_rejects_short_jwt_secret():
    """Verify production mode rejects secrets under 32 characters."""
    cfg = Config(
        environment="production",
        jwt_secret_key="too-short-secret",
        cors_allow_origins=["https://dashboard.recoveryos.com"],
        persistence_backend="in_memory",
    )
    with pytest.raises(ValueError) as exc_info:
        cfg.validate_production_config()
    assert ">= 32 characters" in str(exc_info.value)


def test_production_config_rejects_wildcard_cors():
    """Verify production mode rejects wildcard '*' CORS origins."""
    cfg = Config(
        environment="production",
        jwt_secret_key="a-very-secure-custom-production-secret-key-32bytes",
        cors_allow_origins=["*"],
        persistence_backend="in_memory",
    )
    with pytest.raises(ValueError) as exc_info:
        cfg.validate_production_config()
    assert "Wildcard CORS" in str(exc_info.value)


def test_production_config_rejects_invalid_persistence_backend():
    """Verify production mode rejects invalid persistence backend values."""
    cfg = Config(
        environment="production",
        jwt_secret_key="a-very-secure-custom-production-secret-key-32bytes",
        cors_allow_origins=["https://app.recoveryos.com"],
        persistence_backend="unsupported_database",
    )
    with pytest.raises(ValueError) as exc_info:
        cfg.validate_production_config()
    assert "Unsupported PERSISTENCE_BACKEND" in str(exc_info.value)


def test_production_config_passes_with_secure_settings():
    """Verify production validation succeeds with compliant configuration."""
    cfg = Config(
        environment="production",
        jwt_secret_key="a-very-secure-custom-production-secret-key-32bytes",
        cors_allow_origins=["https://app.recoveryos.com", "https://admin.recoveryos.com"],
        persistence_backend="in_memory",
    )
    # Should not raise
    cfg.validate_production_config()
