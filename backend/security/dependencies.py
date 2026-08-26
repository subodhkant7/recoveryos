"""
FastAPI Security & RBAC Dependencies.

Provides modular dependencies for authentication, role-based authorization,
and tenant-isolated workflow access.
"""

from __future__ import annotations

from typing import Callable
from fastapi import Depends, HTTPException, Header, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from backend.security.principal import Principal, Role, Permission
from backend.security.tokens import verify_access_token, AuthenticationError
from backend.security.audit import record_security_audit_event

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> Principal:
    """
    Extract and verify Bearer JWT token from request header.
    Returns the authenticated Principal.
    Raises HTTPException(401) on missing or invalid credentials.
    """
    if credentials is None or not credentials.credentials:
        record_security_audit_event(
            event_type="AUTH_FAILURE",
            actor_id="anonymous",
            action="authenticate",
            outcome="DENIED",
            reason="Missing Authorization Bearer header",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        principal = verify_access_token(token)
        return principal
    except AuthenticationError as e:
        record_security_audit_event(
            event_type="AUTH_FAILURE",
            actor_id="anonymous",
            action="authenticate",
            outcome="DENIED",
            reason=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_role(*allowed_roles: Role | str) -> Callable[[Principal], Principal]:
    """
    Dependency factory requiring the principal to have one of the specified roles.
    """
    parsed_roles = [Role(r) if isinstance(r, str) else r for r in allowed_roles]

    async def _role_checker(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.role not in parsed_roles and principal.role != Role.ADMIN:
            record_security_audit_event(
                event_type="AUTH_DENIAL",
                actor_id=principal.user_id,
                role=principal.role.value,
                tenant_id=principal.tenant_id,
                action="role_check",
                outcome="DENIED",
                reason=f"Role '{principal.role.value}' not in allowed roles {[r.value for r in parsed_roles]}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this operation",
            )
        return principal

    return _role_checker


def require_permission(permission: Permission) -> Callable[[Principal], Principal]:
    """
    Dependency factory requiring the principal to hold a specific permission.
    """
    async def _permission_checker(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not principal.has_permission(permission):
            record_security_audit_event(
                event_type="AUTH_DENIAL",
                actor_id=principal.user_id,
                role=principal.role.value,
                tenant_id=principal.tenant_id,
                action="permission_check",
                outcome="DENIED",
                reason=f"Role '{principal.role.value}' lacks permission '{permission.value}'",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: insufficient permissions",
            )
        return principal

    return _permission_checker
