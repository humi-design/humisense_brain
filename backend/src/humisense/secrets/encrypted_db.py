"""Encrypted database secrets provider.

Use this only where a managed vault is genuinely unavailable. It is a documented,
bounded substitute, not a vault:

* Confidentiality and integrity come from AES-256-GCM (AEAD) via the ``cryptography``
  library. No home-made construction is used.
* The master key is supplied out of band through configuration, never stored in the
  database. A database dump alone therefore does not disclose credentials.
* Every stored blob binds the tenant and secret name into the AEAD associated data, so a
  ciphertext moved to another tenant's row fails to decrypt instead of silently
  disclosing another tenant's credential.
* ``key_version`` is stored per row so the master key can be rotated incrementally.

Operational requirements are documented in ``docs/SECRETS.md``. Residual risk: an
attacker holding both a database dump and the master key recovers the credentials.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any, cast

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from humisense.db.models import SecretRecord
from humisense.db.session import Database
from humisense.errors import ConfigurationError, SecretDecryptionError, SecretNotFoundError
from humisense.secrets.base import SecretRef

logger = structlog.get_logger(__name__)

ALGORITHM = "AES-256-GCM"
NONCE_BYTES = 12
KEY_BYTES = 32
MAX_SECRET_BYTES = 64 * 1024


def decode_master_key(encoded: str | None) -> bytes:
    """Decode a base64 master key and validate its length.

    Raises:
        ConfigurationError: if the key is missing, malformed, or the wrong size.
    """
    if not encoded:
        raise ConfigurationError(
            detail="secrets_master_key is required by the encrypted_db secrets provider"
        )
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(detail="secrets_master_key is not valid base64") from exc
    if len(key) != KEY_BYTES:
        raise ConfigurationError(
            detail=f"secrets_master_key must decode to exactly {KEY_BYTES} bytes"
        )
    return key


def _affected_rows(result: Any) -> int:
    """Return the number of rows an UPDATE or DELETE affected.

    ``Session.execute`` is typed as returning ``Result[Any]``, which has no ``rowcount``.
    For a DML statement the runtime object is a ``CursorResult``, which does. This keeps
    the cast in one place instead of scattering ignores across call sites.
    """
    return int(cast(CursorResult[Any], result).rowcount or 0)


class EncryptedDbSecretsProvider:
    """Stores credentials as AES-256-GCM ciphertext rows."""

    name = "encrypted_db"

    def __init__(self, database: Database, *, master_key: str | None) -> None:
        self._database = database
        self._key = decode_master_key(master_key)
        self._aead = AESGCM(self._key)
        self._key_version = 1

    @staticmethod
    def _associated_data(ref: SecretRef) -> bytes:
        """Bind the ciphertext to its tenant and name.

        This makes a ciphertext non-transferable: copying a row to another tenant or
        renaming it produces an authentication failure rather than a wrong-tenant read.
        """
        return f"humisense:secret:v1:{ref.organization_id}:{ref.name}".encode()

    async def put(
        self,
        ref: SecretRef,
        value: dict[str, Any],
        *,
        created_by: uuid.UUID | None = None,
    ) -> None:
        if not isinstance(value, dict):
            raise ConfigurationError(detail="A credential value must be a JSON object")
        plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(plaintext) > MAX_SECRET_BYTES:
            raise ConfigurationError(detail=f"Credential exceeds the {MAX_SECRET_BYTES}-byte limit")

        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext, self._associated_data(ref))
        del plaintext

        async with self._database.tenant_session(ref.organization_id) as session:
            existing = await session.scalar(
                select(SecretRecord).where(
                    SecretRecord.organization_id == ref.organization_id,
                    SecretRecord.name == ref.name,
                )
            )
            if existing is not None:
                existing.nonce = nonce
                existing.ciphertext = ciphertext
                existing.algorithm = ALGORITHM
                existing.key_version = self._key_version
                existing.created_by = created_by
            else:
                session.add(
                    SecretRecord(
                        organization_id=ref.organization_id,
                        name=ref.name,
                        algorithm=ALGORITHM,
                        key_version=self._key_version,
                        nonce=nonce,
                        ciphertext=ciphertext,
                        created_by=created_by,
                    )
                )
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConfigurationError(
                    detail="Credential could not be stored due to a constraint violation"
                ) from exc

        logger.info(
            "secret_stored",
            organization_id=str(ref.organization_id),
            secret_name=ref.name,
            algorithm=ALGORITHM,
        )

    async def get(self, ref: SecretRef) -> dict[str, Any]:
        async with self._database.tenant_session(ref.organization_id) as session:
            record = await session.scalar(
                select(SecretRecord).where(
                    SecretRecord.organization_id == ref.organization_id,
                    SecretRecord.name == ref.name,
                )
            )
        if record is None:
            raise SecretNotFoundError(
                detail="No credential is stored for this connection",
                context={"secret_name": ref.name},
            )
        if record.key_version != self._key_version:
            raise SecretDecryptionError(
                detail="Credential was sealed with a key version this process does not hold",
                context={"key_version": record.key_version},
            )
        try:
            plaintext = self._aead.decrypt(
                bytes(record.nonce), bytes(record.ciphertext), self._associated_data(ref)
            )
        except InvalidTag as exc:
            # Wrong key, wrong tenant, or tampered ciphertext. Deliberately identical
            # handling for all three so the failure is not an oracle.
            logger.error(
                "secret_decryption_failed",
                organization_id=str(ref.organization_id),
                secret_name=ref.name,
            )
            raise SecretDecryptionError(
                detail="Stored credential failed authentication and was not used"
            ) from exc
        parsed = json.loads(plaintext)
        if not isinstance(parsed, dict):
            raise SecretDecryptionError(detail="Stored credential is not a JSON object")
        return parsed

    async def delete(self, ref: SecretRef) -> bool:
        async with self._database.tenant_session(ref.organization_id) as session:
            result = await session.execute(
                delete(SecretRecord).where(
                    SecretRecord.organization_id == ref.organization_id,
                    SecretRecord.name == ref.name,
                )
            )
            removed = _affected_rows(result) > 0
        if removed:
            logger.info(
                "secret_deleted",
                organization_id=str(ref.organization_id),
                secret_name=ref.name,
            )
        return removed

    async def exists(self, ref: SecretRef) -> bool:
        async with self._database.tenant_session(ref.organization_id) as session:
            found = await session.scalar(
                select(SecretRecord.id).where(
                    SecretRecord.organization_id == ref.organization_id,
                    SecretRecord.name == ref.name,
                )
            )
        return found is not None
