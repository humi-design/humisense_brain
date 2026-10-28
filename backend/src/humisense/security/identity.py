"""Identity contracts and token verification.

Two verifiers exist:

* :class:`OidcTokenVerifier` — production. Verifies signature against the provider's
  JWKS, and validates issuer, audience, expiry, and not-before.
* :class:`DevTokenVerifier` — development and test only. It issues and accepts tokens
  signed with a local secret so the stack can run without an external identity provider.
  It is refused when ``app_env=production`` (see ``Settings.validate_for_production``).

Neither verifier trusts a client-supplied user id, organization id, or role.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient

from humisense.config import Settings
from humisense.errors import AuthenticationError, ConfigurationError


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Verified claims extracted from an identity token."""

    issuer: str
    subject: str
    email: str | None
    display_name: str | None
    expires_at: int
    issued_at: int
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


class TokenVerifier(Protocol):
    """Verifies an identity token and returns its claims."""

    async def verify(self, token: str) -> TokenClaims: ...


class OidcTokenVerifier:
    """Verifies tokens issued by an OpenID Connect provider."""

    def __init__(self, settings: Settings) -> None:
        if not settings.oidc_issuer or not settings.oidc_audience:
            raise ConfigurationError(
                detail="OIDC verification requires oidc_issuer and oidc_audience"
            )
        self._issuer = settings.oidc_issuer.rstrip("/")
        self._audience = settings.oidc_audience
        self._algorithms = list(settings.oidc_algorithms)
        self._leeway = settings.oidc_leeway_seconds
        jwks_url = settings.oidc_jwks_url or f"{self._issuer}/.well-known/jwks.json"
        # PyJWKClient caches keys and refreshes on an unknown key id.
        self._jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=300)

    async def verify(self, token: str) -> TokenClaims:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
        except Exception as exc:
            raise AuthenticationError(
                detail="Unable to resolve a signing key for the presented token",
                context={"error_type": type(exc).__name__},
            ) from exc
        return self._decode(token, signing_key.key)

    def _decode(self, token: str, key: Any) -> TokenClaims:
        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                # These are mandatory: a token without them is not usable here.
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError(detail="Identity token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError(detail="Identity token audience mismatch") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError(detail="Identity token issuer mismatch") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError(
                detail="Identity token failed verification",
                context={"error_type": type(exc).__name__},
            ) from exc
        return _claims_from_payload(payload, self._issuer)


class DevTokenVerifier:
    """Development-only verifier using a shared HMAC secret.

    Refuses to operate when the environment is production, so a misconfiguration cannot
    silently downgrade authentication.
    """

    ALGORITHM = "HS256"
    ISSUER = "humisense-dev"

    def __init__(self, settings: Settings) -> None:
        if settings.is_production:
            raise ConfigurationError(
                detail="DevTokenVerifier must never be constructed in production"
            )
        self._secret = settings.session_secret_value
        self._audience = "humisense-dev-api"

    def issue(
        self,
        *,
        subject: str,
        email: str | None = None,
        display_name: str | None = None,
    ) -> str:
        """Issue a development token. Test and local development only."""
        now = int(time.time())
        payload = {
            "iss": self.ISSUER,
            "aud": self._audience,
            "sub": subject,
            "email": email,
            "name": display_name,
            "iat": now,
            "exp": now + 3600,
        }
        return jwt.encode(payload, self._secret, algorithm=self.ALGORITHM)

    async def verify(self, token: str) -> TokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self.ALGORITHM],
                audience=self._audience,
                issuer=self.ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError(detail="Development token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError(
                detail="Development token failed verification",
                context={"error_type": type(exc).__name__},
            ) from exc
        return _claims_from_payload(payload, self.ISSUER)


def _claims_from_payload(payload: dict[str, Any], issuer: str) -> TokenClaims:
    subject = payload.get("sub")
    if not subject or not isinstance(subject, str):
        raise AuthenticationError(detail="Identity token is missing a usable subject")
    email = payload.get("email")
    name = payload.get("name") or payload.get("preferred_username")
    return TokenClaims(
        issuer=str(payload.get("iss", issuer)),
        subject=subject,
        email=email if isinstance(email, str) else None,
        display_name=name if isinstance(name, str) else None,
        expires_at=int(payload.get("exp", 0)),
        issued_at=int(payload.get("iat", 0)),
        raw=payload,
    )


def build_token_verifier(settings: Settings) -> TokenVerifier:
    """Return the verifier for the configured authentication mode."""
    if settings.auth_mode == "oidc":
        return OidcTokenVerifier(settings)
    if settings.is_production:
        raise ConfigurationError(detail="auth_mode='dev' is refused in production; configure OIDC")
    return DevTokenVerifier(settings)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated actor for one request, resolved from server-side state.

    ``organization_id`` and ``role`` come from the ``memberships`` table, never from the
    request. ``session_id`` is present for cookie-authenticated requests.
    """

    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: str
    issuer: str
    subject: str
    email: str | None = None
    display_name: str | None = None
    session_id: uuid.UUID | None = None
    auth_method: str = "dev"
    permissions: frozenset[str] = frozenset()

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions
