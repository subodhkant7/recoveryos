"""
Authentication and Credential Verification Subsystem for RecoveryOS.

Provides secure password hashing, constant-time verification, and server-side
authorization binding (preventing client-chosen roles or tenants).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from typing import Any, Optional
from pydantic import BaseModel

from backend.config import config
from backend.security.principal import Role, Principal
from backend.security.tokens import create_access_token

logger = logging.getLogger("recoveryos.security.authenticator")


class UserRecord(BaseModel):
    username: str
    password_hash: str
    salt: str
    role: Role
    tenant_id: str = "tenant-default"
    is_active: bool = True


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """
    Hash a plaintext password using PBKDF2-HMAC-SHA256 with 100,000 iterations.
    Returns (hex_password_hash, hex_salt).
    """
    if salt is None:
        salt_bytes = secrets.token_bytes(16)
        salt_hex = salt_bytes.hex()
    else:
        salt_hex = salt
        salt_bytes = bytes.fromhex(salt_hex)

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_bytes,
        iterations=100000,
    )
    return derived.hex(), salt_hex


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """
    Constant-time password hash verification against PBKDF2-HMAC-SHA256.
    """
    try:
        expected_hash, _ = hash_password(password, salt=salt)
        return hmac.compare_digest(expected_hash, password_hash)
    except Exception:
        return False


class AuthenticationProvider:
    """
    Server-side authentication provider enforcing credential validation
    and deterministic server-side role/tenant binding.
    """

    def __init__(self):
        self._users: dict[str, UserRecord] = {}
        self._init_default_users()

    def _init_default_users(self) -> None:
        """Initialize standard authorized system users with secure password hashes."""
        default_accounts = [
            ("admin", "AdminSecurePass!2026", Role.ADMIN, "tenant-default"),
            ("operator", "OperatorSecurePass!2026", Role.OPERATOR, "tenant-default"),
            ("approver", "ApproverSecurePass!2026", Role.APPROVER, "tenant-default"),
            ("viewer", "ViewerSecurePass!2026", Role.VIEWER, "tenant-default"),
            ("operator-1", "OperatorSecurePass!2026", Role.OPERATOR, "tenant-default"),
            ("admin-1", "AdminSecurePass!2026", Role.ADMIN, "tenant-default"),
            ("approver-1", "ApproverSecurePass!2026", Role.APPROVER, "tenant-default"),
            ("viewer-1", "ViewerSecurePass!2026", Role.VIEWER, "tenant-default"),
            ("operator-acme", "AcmeSecurePass!2026", Role.OPERATOR, "tenant-acme"),
            ("operator-alice", "OperatorSecurePass!2026", Role.OPERATOR, "tenant-corp"),
        ]

        for username, pwd, role, tenant in default_accounts:
            pwd_hash, salt = hash_password(pwd)
            self._users[username] = UserRecord(
                username=username,
                password_hash=pwd_hash,
                salt=salt,
                role=role,
                tenant_id=tenant,
            )

    def register_user(
        self,
        username: str,
        password: str,
        role: Role,
        tenant_id: str = "tenant-default",
    ) -> UserRecord:
        """Register or update a user record with hashed credentials."""
        pwd_hash, salt = hash_password(password)
        record = UserRecord(
            username=username,
            password_hash=pwd_hash,
            salt=salt,
            role=role,
            tenant_id=tenant_id,
        )
        self._users[username] = record
        return record

    def authenticate(
        self,
        username: str,
        password: str,
    ) -> Optional[UserRecord]:
        """
        Authenticate a user by username and password.
        Returns the UserRecord if valid, None otherwise.
        Never reveals whether the username or password was incorrect.
        """
        user = self._users.get(username)
        if not user or not user.is_active:
            # Constant-time dummy verification to mitigate timing attacks
            dummy_salt = "0" * 32
            dummy_hash = "0" * 64
            verify_password(password, dummy_hash, dummy_salt)
            return None

        if not verify_password(password, user.password_hash, user.salt):
            return None

        return user

    def get_active_user(self, username: str) -> Optional[UserRecord]:
        """Return the current server-side identity for refresh-token renewal."""
        user = self._users.get(username)
        return user if user and user.is_active else None


# Singleton authentication provider
auth_provider = AuthenticationProvider()
