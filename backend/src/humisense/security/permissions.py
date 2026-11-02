"""Permission catalogue and the role matrix.

Authorization is deny-by-default: a permission that is not listed for a role is denied.
The matrix is the single source of truth for both the API dependency layer and the
worker, so the two cannot drift apart.
"""

from __future__ import annotations

import enum
from types import MappingProxyType
from typing import Final


class Permission(str, enum.Enum):
    """A capability that can be required of an action."""

    CONNECTORS_READ = "connectors:read"

    CONNECTIONS_READ = "connections:read"
    CONNECTIONS_CREATE = "connections:create"
    CONNECTIONS_UPDATE = "connections:update"
    CONNECTIONS_DELETE = "connections:delete"
    CONNECTIONS_TEST = "connections:test"

    SYNC_READ = "sync:read"
    SYNC_RUN = "sync:run"
    SYNC_CANCEL = "sync:cancel"

    RECORDS_READ = "records:read"
    AUDIT_READ = "audit:read"

    SECRETS_WRITE = "secrets:write"
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    ORGANIZATION_READ = "organization:read"

    METRICS_READ = "metrics:read"


VIEWER_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.CONNECTORS_READ,
        Permission.CONNECTIONS_READ,
        Permission.SYNC_READ,
        Permission.RECORDS_READ,
        Permission.ORGANIZATION_READ,
    }
)

OPERATOR_PERMISSIONS: Final[frozenset[Permission]] = VIEWER_PERMISSIONS | frozenset(
    {
        Permission.CONNECTIONS_TEST,
        Permission.SYNC_RUN,
        Permission.SYNC_CANCEL,
    }
)

ADMIN_PERMISSIONS: Final[frozenset[Permission]] = OPERATOR_PERMISSIONS | frozenset(
    {
        Permission.CONNECTIONS_CREATE,
        Permission.CONNECTIONS_UPDATE,
        Permission.CONNECTIONS_DELETE,
        Permission.AUDIT_READ,
        Permission.SECRETS_WRITE,
        Permission.MEMBERS_READ,
        # Operational visibility is an administrator capability. The registry carries no
        # tenant label, so this grants aggregate process metrics, not tenant data.
        Permission.METRICS_READ,
    }
)

OWNER_PERMISSIONS: Final[frozenset[Permission]] = ADMIN_PERMISSIONS | frozenset(
    {Permission.MEMBERS_MANAGE}
)

ROLE_PERMISSIONS: Final[MappingProxyType[str, frozenset[Permission]]] = MappingProxyType(
    {
        "viewer": VIEWER_PERMISSIONS,
        "operator": OPERATOR_PERMISSIONS,
        "admin": ADMIN_PERMISSIONS,
        "owner": OWNER_PERMISSIONS,
    }
)
"""Role to permission map. Unknown roles resolve to the empty set (deny)."""


def permissions_for_role(role: str) -> frozenset[str]:
    """Return the permission strings granted to ``role``.

    An unrecognized role yields an empty set rather than raising, so a corrupted role
    value in the database degrades to "no access" instead of "unexpected access".
    """
    return frozenset(p.value for p in ROLE_PERMISSIONS.get(role, frozenset()))


def role_has_permission(role: str, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def all_permission_values() -> list[str]:
    return [p.value for p in Permission]
