"""
Principal and Role-Based Access Control (RBAC) domain models.
"""

from __future__ import annotations

from enum import Enum
from typing import Set
from pydantic import BaseModel, Field


class Role(str, Enum):
    """Explicit roles for RecoveryOS API callers."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    ADMIN = "admin"


class Permission(str, Enum):
    """Granular permissions granted to roles."""

    WORKFLOW_READ = "workflow:read"
    WORKFLOW_OPERATE = "workflow:operate"
    WORKFLOW_APPROVE = "workflow:approve"
    ADMIN_ALL = "admin:all"


ROLE_PERMISSIONS: dict[Role, Set[Permission]] = {
    Role.VIEWER: {Permission.WORKFLOW_READ},
    Role.OPERATOR: {Permission.WORKFLOW_READ, Permission.WORKFLOW_OPERATE},
    Role.APPROVER: {Permission.WORKFLOW_READ, Permission.WORKFLOW_APPROVE},
    Role.ADMIN: {
        Permission.WORKFLOW_READ,
        Permission.WORKFLOW_OPERATE,
        Permission.WORKFLOW_APPROVE,
        Permission.ADMIN_ALL,
    },
}


class Principal(BaseModel):
    """
    Authenticated caller identity derived from verified JWT.
    """

    user_id: str
    role: Role
    tenant_id: str = "tenant-default"
    email: str | None = None

    def has_permission(self, permission: Permission) -> bool:
        """Check if principal's role grants the given permission."""
        permissions = ROLE_PERMISSIONS.get(self.role, set())
        return permission in permissions or Permission.ADMIN_ALL in permissions

    def can_access_tenant(self, target_tenant_id: str) -> bool:
        """Admins can access any tenant; other roles are strictly tenant-isolated."""
        if self.role == Role.ADMIN:
            return True
        return self.tenant_id == target_tenant_id
