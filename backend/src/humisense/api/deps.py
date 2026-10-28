"""Authentication and authorization dependencies.

The authenticated principal is resolved from server-side state only:

1. The session cookie holds an opaque random token. Only its SHA-256 digest is stored, so
   a database read does not yield a usable session.
2. The session row names the user and the organization.
3. The organization and role come from the ``memberships`` table.

Nothing in the request body, query string, or headers is used to establish identity,
tenant, or role. A client cannot influence any of the three.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import structlog
from fastapi import Cookie, Depends, Header, Request
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from humisense.config import Settings
from humisense.container import Container
from humisense.db.models import Membership, Session, User
from humisense.errors import AuthenticationError, AuthorizationError, CsrfError
from humisense.security.crypto import (
    constant_time_equals,
    csrf_token_hash,
    new_token,
    session_token_hash,
)
from humisense.security.identity import Principal
from humisense.security.permissions import Permission, permissions_for_role

logger = structlog.get_logger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A freshly created session, including the raw tokens.

    The raw values are returned to the caller exactly once, to be set as cookies. They are
    never persisted in plaintext.
    """

    session_id: uuid.UUID
    session_token: str
    csrf_token: str
    organization_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime


def get_container(request: Request) -> Container:
    """Return the application container attached to the app state."""
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - startup ordering guarantees this
        raise RuntimeError("application container is not initialized")
    return container


def get_settings_dep(container: Annotated[Container, Depends(get_container)]) -> Settings:
    return container.settings


ContainerDep = Annotated[Container, Depends(get_container)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


async def create_session(
    container: Container,
    *,
    claims: Any,
    organization_id: uuid.UUID,
    auth_method: str,
    client_fingerprint: str | None = None,
) -> IssuedSession:
    """Create a user (if needed) and a server-side session.

    The user is keyed by the verified ``(issuer, subject)`` pair, never by an email
    address or a client-supplied id. A user with no active membership in the requested
    organization is refused, so a valid login is not by itself access to a tenant.
    """
    settings = container.settings
    now = datetime.now(UTC)

    async with container.database.system_session() as session:
        user = await session.scalar(
            select(User).where(User.issuer == claims.issuer, User.subject == claims.subject)
        )
        if user is None:
            user = User(
                issuer=claims.issuer,
                subject=claims.subject,
                email=claims.email,
                display_name=claims.display_name,
                last_login_at=now,
            )
            session.add(user)
            await session.flush()
        else:
            if not user.is_active:
                raise AuthenticationError(detail="This identity is disabled")
            user.email = claims.email or user.email
            user.display_name = claims.display_name or user.display_name
            user.last_login_at = now

        membership = await session.scalar(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == organization_id,
                Membership.is_active.is_(True),
            )
        )
        if membership is None:
            raise AuthorizationError(
                detail="This identity is not a member of the requested organization"
            )

        session_token = new_token()
        csrf_token = new_token()
        expires_at = now + timedelta(seconds=settings.session_ttl_seconds)

        record = Session(
            user_id=user.id,
            organization_id=organization_id,
            token_hash=session_token_hash(session_token),
            csrf_hash=csrf_token_hash(csrf_token),
            expires_at=expires_at,
            last_seen_at=now,
            auth_method=auth_method,
            client_fingerprint=client_fingerprint,
        )
        session.add(record)
        await session.flush()
        session_id = record.id
        user_id = user.id

    logger.info(
        "session_created",
        user_id=str(user_id),
        organization_id=str(organization_id),
        auth_method=auth_method,
    )
    return IssuedSession(
        session_id=session_id,
        session_token=session_token,
        csrf_token=csrf_token,
        organization_id=organization_id,
        user_id=user_id,
        expires_at=expires_at,
    )


def _affected_rows(result: Any) -> int:
    """Return the number of rows an UPDATE or DELETE affected.

    ``Session.execute`` is typed as returning ``Result[Any]``, which has no ``rowcount``.
    For a DML statement the runtime object is a ``CursorResult``, which does. This keeps
    the cast in one place instead of scattering ignores across call sites.
    """
    return int(cast(CursorResult[Any], result).rowcount or 0)


async def revoke_session(container: Container, session_token: str) -> bool:
    """Revoke a session by its raw token."""
    digest = session_token_hash(session_token)
    async with container.database.system_session() as session:
        result = await session.execute(
            update(Session)
            .where(Session.token_hash == digest, Session.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        return _affected_rows(result) > 0


async def resolve_principal(container: Container, session_token: str | None) -> Principal:
    """Resolve the authenticated principal from a session cookie.

    Raises:
        AuthenticationError: when the cookie is missing, unknown, expired, idle, or
            revoked. All four cases produce the same client-visible error, so the API does
            not disclose which one occurred.
    """
    if not session_token:
        raise AuthenticationError(detail="No session cookie was presented")

    settings = container.settings
    digest = session_token_hash(session_token)
    now = datetime.now(UTC)

    async with container.database.system_session() as session:
        record = await session.scalar(select(Session).where(Session.token_hash == digest))
        if record is None:
            raise AuthenticationError(detail="Session is not recognized")
        if record.revoked_at is not None:
            raise AuthenticationError(detail="Session was revoked")
        if record.expires_at <= now:
            raise AuthenticationError(detail="Session has expired")
        idle_limit = timedelta(seconds=settings.session_idle_timeout_seconds)
        if now - record.last_seen_at > idle_limit:
            raise AuthenticationError(detail="Session expired due to inactivity")

        membership = await session.scalar(
            select(Membership).where(
                Membership.user_id == record.user_id,
                Membership.organization_id == record.organization_id,
                Membership.is_active.is_(True),
            )
        )
        if membership is None:
            raise AuthenticationError(detail="Membership is no longer active")

        user = await session.scalar(select(User).where(User.id == record.user_id))
        if user is None or not user.is_active:
            raise AuthenticationError(detail="Identity is disabled")

        # Idle tracking: only refresh when a meaningful amount of time has passed, so a
        # write does not happen on every request.
        if now - record.last_seen_at > timedelta(seconds=60):
            record.last_seen_at = now

        principal = Principal(
            user_id=user.id,
            organization_id=record.organization_id,
            role=membership.role.value,
            issuer=user.issuer,
            subject=user.subject,
            email=user.email,
            display_name=user.display_name,
            session_id=record.id,
            auth_method=record.auth_method,
            permissions=permissions_for_role(membership.role.value),
        )
    return principal


async def get_principal(
    request: Request,
    container: ContainerDep,
    session_cookie: Annotated[str | None, Cookie(alias="humisense_session")] = None,
) -> Principal:
    """FastAPI dependency that yields the authenticated principal."""
    principal = await resolve_principal(container, session_cookie)
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def enforce_csrf(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Enforce double-submit CSRF protection on unsafe methods.

    The header value must match the CSRF digest bound to the session. The token is never
    placed in a cookie the client script can read for this purpose; the frontend obtains
    it from a dedicated endpoint that requires the session cookie.

    The comparison is constant time, so the token cannot be recovered byte by byte.
    """
    if request.method in SAFE_METHODS:
        return
    if not csrf_header:
        raise CsrfError(detail="The CSRF token header is missing")
    if principal.session_id is None:
        raise CsrfError(detail="Session has no CSRF binding")

    async with container.database.system_session() as session:
        record = await session.scalar(select(Session).where(Session.id == principal.session_id))
        if record is None:
            raise CsrfError(detail="Session is no longer valid")
        expected = record.csrf_hash
    if not constant_time_equals(expected, csrf_token_hash(csrf_header)):
        raise CsrfError(detail="The CSRF token did not match")


def require_permission(permission: Permission) -> Any:
    """Build a dependency that enforces a permission server-side."""

    async def _dependency(principal: PrincipalDep) -> Principal:
        if permission.value not in principal.permissions:
            raise AuthorizationError(
                detail="Your role does not permit this action",
                context={"required_permission": permission.value, "role": principal.role},
            )
        return principal

    return _dependency


def require_permissions(*permissions: Permission) -> Any:
    """Build a dependency requiring every listed permission."""

    async def _dependency(principal: PrincipalDep) -> Principal:
        missing = [p.value for p in permissions if p.value not in principal.permissions]
        if missing:
            raise AuthorizationError(
                detail="Your role does not permit this action",
                context={"missing_permissions": missing, "role": principal.role},
            )
        return principal

    return _dependency
