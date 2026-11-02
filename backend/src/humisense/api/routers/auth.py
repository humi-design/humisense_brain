"""Session endpoints.

Sign-in flow for the development identity provider:

1. ``POST /auth/session`` accepts an identity token. In ``oidc`` mode this is the token
   the frontend obtained from the provider through an authorization-code + PKCE flow. In
   ``dev`` mode the body names a seeded development identity, which the server maps to a
   token it issues itself; the client cannot assert a user id, tenant, or role.
2. The server verifies the token, resolves the user by ``(issuer, subject)``, checks an
   active membership, and creates a server-side session.
3. The session cookie is set ``HttpOnly``, ``SameSite=Lax``, and ``Secure`` in production.
   A separate CSRF cookie is set for the double-submit check.

Logout revokes the session server-side and clears both cookies. No token is ever placed
in browser storage.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from humisense.api.deps import (
    ContainerDep,
    PrincipalDep,
    SettingsDep,
    create_session,
    revoke_session,
)
from humisense.api.schemas import CurrentUserResponse
from humisense.db.models import Organization
from humisense.db.models import Session as SessionModel
from humisense.errors import AuthenticationError, AuthorizationError, ValidationFailure
from humisense.security.crypto import csrf_token_hash, new_token
from humisense.security.headers import client_ip, is_secure_request
from humisense.security.identity import DevTokenVerifier

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])


class DevSignInRequest(BaseModel):
    """Development-only sign-in.

    ``organization_slug`` selects the tenant the operator wants to act in. It is not a
    claim of authorization: the server still requires an active membership row.
    """

    model_config = ConfigDict(extra="forbid")

    subject: Annotated[str, Field(min_length=1, max_length=255)]
    organization_slug: Annotated[str, Field(min_length=1, max_length=80)]
    email: Annotated[str | None, Field(max_length=320)] = None
    display_name: Annotated[str | None, Field(max_length=200)] = None


class OidcSignInRequest(BaseModel):
    """Production sign-in: exchange a provider token for a session."""

    model_config = ConfigDict(extra="forbid")

    id_token: Annotated[str, Field(min_length=1, max_length=8192)]
    organization_id: uuid.UUID


class SessionInfoResponse(BaseModel):
    session_id: str
    expires_at: str
    organization_id: str
    csrf_token: str
    """Returned so the frontend can echo it in the ``X-CSRF-Token`` header. It is bound to
    the session and is useless without the session cookie, which the frontend cannot read."""


def _problems(exc: PydanticValidationError) -> list[dict[str, str]]:
    """Describe validation problems without echoing the submitted values."""
    return [
        {
            "location": ".".join(str(part) for part in error.get("loc", ())),
            "message": error.get("msg", "invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()[:10]
    ]


def _set_session_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    settings: Any,
    secure: bool,
) -> None:
    max_age = settings.session_ttl_seconds
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    # The CSRF cookie is readable by the frontend so it can echo the value in a header. It
    # is not a bearer credential: the HttpOnly session cookie is what authenticates.
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookies(response: Response, settings: Any) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


async def _organization_name(container: Any, organization_id: uuid.UUID) -> str:
    async with container.database.system_session() as session:
        name = await session.scalar(
            select(Organization.name).where(Organization.id == organization_id)
        )
    return str(name or "Unknown organization")


@router.post(
    "/session",
    response_model=SessionInfoResponse,
    summary="Create a session",
)
async def create_session_endpoint(
    request: Request,
    response: Response,
    container: ContainerDep,
    settings: SettingsDep,
    payload: Annotated[dict[str, Any], Body()],
) -> SessionInfoResponse:
    """Create a session from a verified identity."""
    secure = (
        is_secure_request(request, trusted_proxy_count=settings.trusted_proxy_count)
        or settings.effective_cookie_secure
    )

    if settings.auth_mode == "oidc":
        try:
            oidc_request = OidcSignInRequest.model_validate(payload)
        except PydanticValidationError as exc:
            raise ValidationFailure(
                detail="The sign-in request was not accepted",
                context={"problems": _problems(exc)},
            ) from exc
        claims = await container.token_verifier.verify(oidc_request.id_token)
        organization_id = oidc_request.organization_id
        async with container.database.system_session() as session:
            exists = await session.scalar(
                select(Organization.id).where(Organization.id == organization_id)
            )
        if exists is None:
            # Do not disclose whether the organization exists.
            raise AuthorizationError(detail="Sign-in was refused for this identity")
        auth_method = "oidc"
    else:
        try:
            dev_request = DevSignInRequest.model_validate(payload)
        except PydanticValidationError as exc:
            # An unknown field is refused rather than ignored, so a client cannot attempt
            # to assert a role, a tenant, or any other privileged input.
            raise ValidationFailure(
                detail="The sign-in request was not accepted",
                context={"problems": _problems(exc)},
            ) from exc
        verifier = container.token_verifier
        if not isinstance(verifier, DevTokenVerifier):
            raise AuthenticationError(detail="Development sign-in is not available")
        async with container.database.system_session() as session:
            dev_organization_id = await session.scalar(
                select(Organization.id).where(Organization.slug == dev_request.organization_slug)
            )
        if dev_organization_id is None:
            raise AuthenticationError(detail="Sign-in was refused for this identity")
        organization_id = dev_organization_id
        token = verifier.issue(
            subject=dev_request.subject,
            email=dev_request.email,
            display_name=dev_request.display_name,
        )
        claims = await verifier.verify(token)
        auth_method = "dev"

    issued = await create_session(
        container,
        claims=claims,
        organization_id=organization_id,
        auth_method=auth_method,
    )
    _set_session_cookies(
        response,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
        settings=settings,
        secure=secure,
    )
    logger.info(
        "sign_in_succeeded",
        auth_method=auth_method,
        organization_id=str(issued.organization_id),
        client_ip=client_ip(request, trusted_proxy_count=settings.trusted_proxy_count),
    )
    return SessionInfoResponse(
        session_id=str(issued.session_id),
        expires_at=issued.expires_at.isoformat(),
        organization_id=str(issued.organization_id),
        csrf_token=issued.csrf_token,
    )


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="Return the authenticated principal",
)
async def read_current_user(
    container: ContainerDep,
    principal: PrincipalDep,
) -> CurrentUserResponse:
    """Return the caller's identity, tenant, role, and effective permissions."""
    name = await _organization_name(container, principal.organization_id)
    return CurrentUserResponse(
        user_id=principal.user_id,
        display_name=principal.display_name,
        email=principal.email,
        organization_id=principal.organization_id,
        organization_name=name,
        role=principal.role,
        permissions=sorted(principal.permissions),
        auth_method=principal.auth_method,
    )


@router.get(
    "/csrf",
    summary="Return a CSRF token bound to the current session",
)
async def read_csrf_token(
    container: ContainerDep,
    principal: PrincipalDep,
) -> dict[str, str]:
    """Issue a fresh CSRF token for the current session.

    Requires a valid session cookie, so an unauthenticated caller cannot obtain one. The
    token is rotated on each call, which also lets a reloaded page recover after the
    original value is gone.
    """
    if principal.session_id is None:
        raise AuthenticationError(detail="Session has no CSRF binding")
    token = new_token()
    async with container.database.system_session() as session:
        stored = await session.scalar(
            select(SessionModel).where(SessionModel.id == principal.session_id)
        )
        if stored is None:
            raise AuthenticationError(detail="Session is no longer valid")
        stored.csrf_hash = csrf_token_hash(token)
    return {"csrf_token": token}


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session",
)
async def logout(
    request: Request,
    response: Response,
    container: ContainerDep,
    settings: SettingsDep,
) -> Response:
    """Revoke the session server-side and clear the cookies."""
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await revoke_session(container, token)
    _clear_session_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
