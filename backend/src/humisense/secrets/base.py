"""Secrets provider abstraction.

Design rules enforced by this module:

* A secret value is write-only. There is no API that returns a stored secret to a client.
* Secrets are addressed by a :class:`SecretRef` that always carries the owning
  organization, so a reference cannot be used across a tenant boundary.
* Credential material is never a plain column value in PostgreSQL when the
  ``encrypted_db`` provider is used; the value is sealed with AES-256-GCM.
* The provider is replaceable. A production deployment is expected to back this with a
  managed vault; the interface is what connectors depend on, not the implementation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A pointer to a stored credential.

    The name is a stable identifier, not a path and not a value. ``organization_id``
    scopes the reference to a tenant.
    """

    name: str
    organization_id: uuid.UUID

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 255:
            raise ValueError("secret name must be between 1 and 255 characters")
        if any(ch in self.name for ch in ("/", "\\", "\x00")):
            raise ValueError("secret name must not contain path separators")

    @property
    def key(self) -> str:
        return f"{self.organization_id}/{self.name}"


class SecretsProvider(Protocol):
    """Stores and resolves connector credentials."""

    name: str

    async def put(
        self,
        ref: SecretRef,
        value: dict[str, Any],
        *,
        created_by: uuid.UUID | None = None,
    ) -> None:
        """Create or replace the credential at ``ref``."""
        ...

    async def get(self, ref: SecretRef) -> dict[str, Any]:
        """Resolve the credential at ``ref``.

        Raises:
            SecretNotFoundError: when the credential does not exist for this tenant.
        """
        ...

    async def delete(self, ref: SecretRef) -> bool:
        """Revoke the credential at ``ref``. Returns True when a value was removed."""
        ...

    async def exists(self, ref: SecretRef) -> bool:
        """Return whether a credential is present, without decrypting it."""
        ...
