"""Environment-backed secrets provider.

Intended for local development and for bootstrapping configuration that must exist
before a vault is reachable. It is a real implementation, not a stub: it reads
``HUMISENSE_SECRET_<ORG_SLUG_UPPER>_<NAME_UPPER>`` and parses a JSON object.

It is **not** suitable as the production store for customer connector credentials: it
requires the operator to place credential material in the process environment, where it
is visible to anything that can read ``/proc``. Production deployments should use a
vault-backed provider.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import structlog

from humisense.errors import ConfigurationError, SecretNotFoundError
from humisense.secrets.base import SecretRef

logger = structlog.get_logger(__name__)


class EnvSecretsProvider:
    """Reads credentials from environment variables. Read-only."""

    name = "env"

    def __init__(self, *, prefix: str = "HUMISENSE_SECRET_") -> None:
        self._prefix = prefix

    def _env_key(self, ref: SecretRef, org_slug: str | None = None) -> str:
        org = (org_slug or str(ref.organization_id)).upper().replace("-", "_")
        name = ref.name.upper().replace("-", "_")
        return f"{self._prefix}{org}__{name}"

    async def put(
        self,
        ref: SecretRef,
        value: dict[str, Any],
        *,
        created_by: uuid.UUID | None = None,
    ) -> None:
        raise ConfigurationError(
            detail="The environment secrets provider is read-only; use the encrypted DB "
            "provider or a vault-backed provider to store credentials",
            context={"provider": self.name},
        )

    async def get(self, ref: SecretRef) -> dict[str, Any]:
        raw = os.environ.get(self._env_key(ref))
        if raw is None:
            raise SecretNotFoundError(
                detail="Credential is not present in the environment for this tenant",
                context={"secret_name": ref.name, "provider": self.name},
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                detail="Environment credential is not valid JSON",
                context={"secret_name": ref.name},
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigurationError(
                detail="Environment credential must be a JSON object",
                context={"secret_name": ref.name},
            )
        return parsed

    async def delete(self, ref: SecretRef) -> bool:
        raise ConfigurationError(detail="The environment secrets provider is read-only")

    async def exists(self, ref: SecretRef) -> bool:
        return self._env_key(ref) in os.environ
