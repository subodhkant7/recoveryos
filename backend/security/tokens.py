"""
JWT token creation and verification subsystem for RecoveryOS.

Provides secure HMAC-SHA256 JWT generation and validation without leaking secrets or payload details.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from backend.config import config
from backend.security.principal import Principal, Role


class AuthenticationError(Exception):
    """Raised when authentication fails due to missing, invalid, or expired tokens."""
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + padding)


def create_access_token(
    user_id: str,
    role: Role | str,
    tenant_id: str = "tenant-default",
    expires_delta: timedelta | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
) -> str:
    """
    Generate a cryptographically signed HMAC-SHA256 JWT access token.
    """
    secret = (secret_key or config.jwt_secret_key).encode("utf-8")
    algo = algorithm or config.jwt_algorithm

    if isinstance(role, Role):
        role_val = role.value
    else:
        role_val = str(role)

    now = int(time.time())
    exp_seconds = int(expires_delta.total_seconds()) if expires_delta else (config.jwt_expiration_minutes * 60)
    exp = now + exp_seconds

    header = {"alg": algo, "typ": "JWT"}
    payload = {
        "token_type": "access",
        "sub": user_id,
        "role": role_val,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": exp,
    }

    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    unsigned = f"{_b64url_encode(header_bytes)}.{_b64url_encode(payload_bytes)}"
    signature = hmac.new(secret, unsigned.encode("ascii"), hashlib.sha256).digest()
    return f"{unsigned}.{_b64url_encode(signature)}"


def create_refresh_token(
    user_id: str,
    role: Role | str,
    tenant_id: str = "tenant-default",
    expires_delta: timedelta | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
) -> str:
    """Generate a longer-lived signed token used only to mint access tokens."""
    secret = (secret_key or config.jwt_secret_key).encode("utf-8")
    algo = algorithm or config.jwt_algorithm
    role_val = role.value if isinstance(role, Role) else str(role)
    now = int(time.time())
    exp_seconds = int(expires_delta.total_seconds()) if expires_delta else (
        config.jwt_refresh_expiration_days * 24 * 60 * 60
    )
    payload = {
        "token_type": "refresh",
        "sub": user_id,
        "role": role_val,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + exp_seconds,
    }
    header = {"alg": algo, "typ": "JWT"}
    unsigned = ".".join(
        _b64url_encode(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, payload)
    )
    signature = hmac.new(secret, unsigned.encode("ascii"), hashlib.sha256).digest()
    return f"{unsigned}.{_b64url_encode(signature)}"


def verify_access_token(
    token: str,
    secret_key: str | None = None,
) -> Principal:
    """
    Verify signature, expiration, and payload of a JWT token.
    Returns the authenticated Principal.
    Raises AuthenticationError on any validation failure.
    """
    secret = (secret_key or config.jwt_secret_key).encode("utf-8")

    if not token or not isinstance(token, str):
        raise AuthenticationError("Missing or invalid token format")

    parts = token.strip().split(".")
    if len(parts) != 3:
        raise AuthenticationError("Malformed token structure")

    header_b64, payload_b64, signature_b64 = parts

    try:
        header_bytes = _b64url_decode(header_b64)
        header = json.loads(header_bytes.decode("utf-8"))
        if header.get("alg") != "HS256":
            raise AuthenticationError("Unsupported signing algorithm")
    except Exception:
        raise AuthenticationError("Invalid token header")

    unsigned = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(secret, unsigned, hashlib.sha256).digest()

    try:
        actual_sig = _b64url_decode(signature_b64)
    except Exception:
        raise AuthenticationError("Invalid token signature encoding")

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise AuthenticationError("Invalid token signature")

    try:
        payload_bytes = _b64url_decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise AuthenticationError("Invalid token payload")

    token_type = payload.get("token_type", "access")
    if token_type != "access":
        raise AuthenticationError("Invalid access token type")

    # Verify expiration
    exp = payload.get("exp")
    if not exp or not isinstance(exp, (int, float)):
        raise AuthenticationError("Token missing expiration claim")

    now = int(time.time())
    if now >= exp:
        raise AuthenticationError("Token has expired")

    # Verify subject and role
    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationError("Token missing subject claim")

    role_str = payload.get("role")
    try:
        role = Role(role_str)
    except (ValueError, KeyError):
        raise AuthenticationError(f"Invalid role claim in token: '{role_str}'")

    tenant_id = payload.get("tenant_id", "tenant-default")

    return Principal(
        user_id=user_id,
        role=role,
        tenant_id=tenant_id,
    )


def verify_refresh_token(token: str, secret_key: str | None = None) -> Principal:
    """Verify a refresh token and return the identity it is allowed to renew."""
    payload_principal = _verify_token_payload(token, secret_key=secret_key)
    if payload_principal[0].get("token_type") != "refresh":
        raise AuthenticationError("Invalid refresh token type")
    return payload_principal[1]


def _verify_token_payload(
    token: str,
    secret_key: str | None = None,
) -> tuple[dict[str, Any], Principal]:
    """Shared signature, expiry, and claim validation for access/refresh tokens."""
    secret = (secret_key or config.jwt_secret_key).encode("utf-8")
    if not token or not isinstance(token, str):
        raise AuthenticationError("Missing or invalid token format")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise AuthenticationError("Malformed token structure")
    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        if header.get("alg") != "HS256":
            raise AuthenticationError("Unsupported signing algorithm")
    except AuthenticationError:
        raise
    except Exception:
        raise AuthenticationError("Invalid token header")
    unsigned = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(secret, unsigned, hashlib.sha256).digest()
    try:
        actual_sig = _b64url_decode(signature_b64)
    except Exception:
        raise AuthenticationError("Invalid token signature encoding")
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise AuthenticationError("Invalid token signature")
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        raise AuthenticationError("Invalid token payload")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or int(time.time()) >= exp:
        raise AuthenticationError("Token has expired")
    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationError("Token missing subject claim")
    try:
        role = Role(payload.get("role"))
    except (ValueError, KeyError):
        raise AuthenticationError(f"Invalid role claim in token: '{payload.get('role')}'")
    return payload, Principal(
        user_id=user_id,
        role=role,
        tenant_id=payload.get("tenant_id", "tenant-default"),
    )
