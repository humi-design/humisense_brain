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

    # Layer 2. Reading ingestion batches and quarantine is visibility; replaying a
    # quarantined record re-runs processing and can create canonical records, so it is an
    # administrator capability.
    INGESTION_READ = "ingestion:read"
    INGESTION_REPLAY = "ingestion:replay"

    # Layer 3. Reading detections and quality is visibility; running an analytics job
    # consumes compute and writes detections, so it is an operator capability; configuring
    # rules changes what the platform considers anomalous, so it is an administrator one.
    ANALYTICS_READ = "analytics:read"
    ANALYTICS_RUN = "analytics:run"
    ANALYTICS_CONFIGURE = "analytics:configure"

    # Layer 4. Reading work is visibility. Changing work state (status, ownership, an
    # authorized conflict resolution) is an operator capability because it alters the
    # organization's record of who is responsible for what.
    WORK_READ = "work:read"
    WORK_MANAGE = "work:manage"

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
        Permission.INGESTION_READ,
        Permission.ANALYTICS_READ,
        Permission.WORK_READ,
    }
)

OPERATOR_PERMISSIONS: Final[frozenset[Permission]] = VIEWER_PERMISSIONS | frozenset(
    {
        Permission.CONNECTIONS_TEST,
        Permission.SYNC_RUN,
        Permission.SYNC_CANCEL,
        Permission.ANALYTICS_RUN,
        Permission.WORK_MANAGE,
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
        Permission.INGESTION_REPLAY,
        Permission.ANALYTICS_CONFIGURE,
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
