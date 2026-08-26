"""
Security and RBAC package for RecoveryOS.
"""

from backend.security.principal import Role, Permission, Principal
from backend.security.tokens import create_access_token, verify_access_token, AuthenticationError
from backend.security.dependencies import get_current_principal, require_role, require_permission
from backend.security.audit import record_security_audit_event, get_security_audit_logs, clear_security_audit_logs

__all__ = [
    "Role",
    "Permission",
    "Principal",
    "create_access_token",
    "verify_access_token",
    "AuthenticationError",
    "get_current_principal",
    "require_role",
    "require_permission",
    "record_security_audit_event",
    "get_security_audit_logs",
    "clear_security_audit_logs",
]
