"""Secrets handling, authenticated encryption, and redaction tests.

Covers T4 (credential theft) and part of T13 (sensitive data in logs and responses).
"""

from __future__ import annotations

import base64
import os
import uuid
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from humisense.container import Container
from humisense.errors import (
    ConfigurationError,
    SecretDecryptionError,
    SecretNotFoundError,
)
from humisense.logging_setup import (
    REDACTED,
    SENSITIVE_KEY_PATTERN,
    redact_mapping,
    redact_text,
    redact_value,
)
from humisense.secrets.base import SecretRef
from humisense.secrets.encrypted_db import (
    KEY_BYTES,
    MAX_SECRET_BYTES,
    EncryptedDbSecretsProvider,
    decode_master_key,
)
from humisense.secrets.env_provider import EnvSecretsProvider
from humisense.secrets.factory import build_secrets_provider
from humisense.security.crypto import (
    constant_time_equals,
    content_hash,
    csrf_token_hash,
    new_token,
    session_token_hash,
)

pytestmark = pytest.mark.security


def a_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def a_ref(organization_id: uuid.UUID, **overrides: object) -> SecretRef:
    """Build a reference for a real organization.

    The ``secrets`` table has a foreign key to ``organizations``, so the identifier must
    name an organization that exists.
    """
    defaults: dict[str, object] = {
        "name": f"conn-{uuid.uuid4().hex[:12]}",
        "organization_id": organization_id,
    }
    defaults.update(overrides)
    return SecretRef(**defaults)  # type: ignore[arg-type]


# --- Master key handling -------------------------------------------------


def test_valid_master_key_is_decoded() -> None:
    assert len(decode_master_key(a_key())) == KEY_BYTES


@pytest.mark.parametrize("bad_key", [None, "", "not-base64!!", "c2hvcnQ=", "YWJj"])
def test_invalid_master_key_is_rejected(bad_key: str | None) -> None:
    with pytest.raises(ConfigurationError):
        decode_master_key(bad_key)


def test_master_key_of_wrong_length_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        decode_master_key(base64.b64encode(b"x" * 16).decode())


def test_encrypted_provider_requires_a_master_key() -> None:
    with pytest.raises(ConfigurationError):
        EncryptedDbSecretsProvider(None, master_key=None)  # type: ignore[arg-type]


# --- Secret reference scoping -------------------------------------------


@pytest.mark.parametrize("bad_name", ["", "a/b", "a\\b", "a\x00b", "x" * 300])
def test_secret_ref_rejects_unsafe_names(bad_name: str) -> None:
    with pytest.raises(ValueError):
        SecretRef(name=bad_name, organization_id=uuid.uuid4())


def test_secret_ref_key_includes_the_tenant() -> None:
    organization_id = uuid.uuid4()
    ref = SecretRef(name="my-credential", organization_id=organization_id)
    assert str(organization_id) in ref.key
    assert ref.key.endswith("my-credential")


def test_secret_ref_is_immutable() -> None:
    ref = a_ref(uuid.uuid4())
    # FrozenInstanceError subclasses AttributeError, so an ordinary caller cannot
    # rebind the name and point a stored reference at a different credential.
    with pytest.raises(FrozenInstanceError):
        ref.name = "changed"  # type: ignore[misc]


# --- Encrypted database provider ----------------------------------------


async def test_encrypted_provider_round_trip(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    provider = container.secrets_provider
    assert isinstance(provider, EncryptedDbSecretsProvider)
    ref = a_ref(organization)
    await provider.put(ref, {"api_key": "the-secret-value"}, created_by=owner.user_id)
    assert await provider.get(ref) == {"api_key": "the-secret-value"}


async def test_encrypted_provider_stores_no_plaintext(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    """T4: the database must not hold the credential in any readable form."""
    distinctive = "zzz-distinctive-credential-zzz"
    ref = a_ref(organization)
    await container.secrets_provider.put(ref, {"api_key": distinctive}, created_by=owner.user_id)

    # A tenant session is required to read the row at all: under a system session the
    # RLS policy matches nothing, which is itself the isolation control.
    async with container.database.tenant_session(organization) as session:
        rows = (await session.execute(text("SELECT ciphertext, nonce, name FROM secrets"))).all()

    assert rows
    for ciphertext, nonce, name in rows:
        assert distinctive.encode() not in bytes(ciphertext)
        assert distinctive.encode() not in bytes(nonce)
        assert distinctive not in name


async def test_encrypted_provider_uses_a_fresh_nonce_per_write(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    """A repeated nonce under one key is catastrophic for GCM."""
    ref = a_ref(organization)
    await container.secrets_provider.put(ref, {"api_key": "same-value"}, created_by=owner.user_id)
    async with container.database.tenant_session(organization) as session:
        first = await session.scalar(
            text("SELECT nonce FROM secrets WHERE name = :n"), {"n": ref.name}
        )

    await container.secrets_provider.put(ref, {"api_key": "same-value"}, created_by=owner.user_id)
    async with container.database.tenant_session(organization) as session:
        second = await session.scalar(
            text("SELECT nonce FROM secrets WHERE name = :n"), {"n": ref.name}
        )

    assert bytes(first) != bytes(second)


async def test_encrypted_provider_upsert_replaces_value(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    ref = a_ref(organization)
    await container.secrets_provider.put(ref, {"api_key": "first"}, created_by=owner.user_id)
    await container.secrets_provider.put(ref, {"api_key": "second"}, created_by=owner.user_id)

    assert await container.secrets_provider.get(ref) == {"api_key": "second"}
    async with container.database.tenant_session(organization) as session:
        count = await session.scalar(
            text("SELECT count(*) FROM secrets WHERE name = :n"), {"n": ref.name}
        )
    assert count == 1


async def test_ciphertext_is_not_transferable_between_names(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    """T1/T4: the AEAD binds the tenant and the name, so a moved row fails to decrypt.

    Without this binding, an attacker with write access to ``secrets`` could copy one
    row's ciphertext into another row and read the credential.
    """
    source_ref = a_ref(organization)
    await container.secrets_provider.put(
        source_ref, {"api_key": "tenant-a-credential"}, created_by=owner.user_id
    )
    target_ref = SecretRef(
        name=f"conn-{uuid.uuid4().hex[:12]}",
        organization_id=source_ref.organization_id,
    )
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text("UPDATE secrets SET name = :new WHERE name = :old"),
            {"new": target_ref.name, "old": source_ref.name},
        )

    with pytest.raises(SecretDecryptionError):
        await container.secrets_provider.get(target_ref)


async def test_tampered_ciphertext_is_rejected(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    ref = a_ref(organization)
    await container.secrets_provider.put(ref, {"api_key": "value"}, created_by=owner.user_id)

    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "UPDATE secrets SET ciphertext = "
                "set_byte(ciphertext, 0, (get_byte(ciphertext, 0) # 1)) WHERE name = :n"
            ),
            {"n": ref.name},
        )

    with pytest.raises(SecretDecryptionError):
        await container.secrets_provider.get(ref)


async def test_unknown_key_version_is_reported(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    """A credential sealed by a newer key version must fail loudly, not silently."""
    ref = a_ref(organization)
    await container.secrets_provider.put(ref, {"api_key": "v"}, created_by=owner.user_id)
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text("UPDATE secrets SET key_version = 99 WHERE name = :n"), {"n": ref.name}
        )
    with pytest.raises(SecretDecryptionError):
        await container.secrets_provider.get(ref)


async def test_delete_revokes_the_credential(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    ref = a_ref(organization)
    await container.secrets_provider.put(ref, {"api_key": "v"}, created_by=owner.user_id)

    assert await container.secrets_provider.delete(ref) is True
    with pytest.raises(SecretNotFoundError):
        await container.secrets_provider.get(ref)
    assert await container.secrets_provider.delete(ref) is False


async def test_exists_reports_presence_without_decrypting(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    ref = a_ref(organization)
    assert await container.secrets_provider.exists(ref) is False
    await container.secrets_provider.put(ref, {"api_key": "v"}, created_by=owner.user_id)
    assert await container.secrets_provider.exists(ref) is True


async def test_missing_credential_raises(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    with pytest.raises(SecretNotFoundError):
        await container.secrets_provider.get(a_ref(organization))


async def test_oversized_credential_is_rejected(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    with pytest.raises(ConfigurationError):
        await container.secrets_provider.put(
            a_ref(organization),
            {"blob": "x" * (MAX_SECRET_BYTES + 1)},
            created_by=owner.user_id,
        )


async def test_non_object_credential_is_rejected(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    with pytest.raises(ConfigurationError):
        await container.secrets_provider.put(
            a_ref(organization),
            ["not", "an", "object"],
            created_by=owner.user_id,  # type: ignore[arg-type]
        )


async def test_secret_error_does_not_echo_the_credential(
    container: Container, organization: uuid.UUID, owner: Any
) -> None:
    ref = a_ref(organization)
    with pytest.raises(SecretNotFoundError) as excinfo:
        await container.secrets_provider.get(ref)
    assert "api_key" not in str(excinfo.value)
    assert ref.name not in excinfo.value.public_message


# --- Environment provider ------------------------------------------------


async def test_env_provider_reads_a_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = a_ref(uuid.uuid4())
    provider = EnvSecretsProvider()
    monkeypatch.setenv(provider._env_key(ref), '{"api_key": "from-environment"}')
    assert await provider.get(ref) == {"api_key": "from-environment"}


async def test_env_provider_reports_absent_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EnvSecretsProvider()
    ref = a_ref(uuid.uuid4())
    monkeypatch.delenv(provider._env_key(ref), raising=False)
    with pytest.raises(SecretNotFoundError):
        await provider.get(ref)


async def test_env_provider_rejects_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = EnvSecretsProvider()
    ref = a_ref(uuid.uuid4())
    monkeypatch.setenv(provider._env_key(ref), "not-json")
    with pytest.raises(ConfigurationError):
        await provider.get(ref)


async def test_env_provider_rejects_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = EnvSecretsProvider()
    ref = a_ref(uuid.uuid4())
    monkeypatch.setenv(provider._env_key(ref), '"a-string"')
    with pytest.raises(ConfigurationError):
        await provider.get(ref)


async def test_env_provider_is_read_only() -> None:
    provider = EnvSecretsProvider()
    with pytest.raises(ConfigurationError):
        await provider.put(a_ref(uuid.uuid4()), {"api_key": "v"}, created_by=uuid.uuid4())
    with pytest.raises(ConfigurationError):
        await provider.delete(a_ref(uuid.uuid4()))


async def test_env_provider_is_tenant_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential set for one tenant must not resolve for another."""
    provider = EnvSecretsProvider()
    first = a_ref(uuid.uuid4())
    monkeypatch.setenv(provider._env_key(first), '{"api_key": "tenant-a"}')

    second = SecretRef(name=first.name, organization_id=uuid.uuid4())
    with pytest.raises(SecretNotFoundError):
        await provider.get(second)


def test_factory_rejects_unknown_provider() -> None:
    """The provider name is a closed set, so an unknown value cannot be configured."""

    from humisense.config import Settings

    with pytest.raises(ValidationError):
        Settings(app_env="test", secrets_provider="plaintext")


def test_factory_selects_the_configured_provider() -> None:
    from humisense.config import Settings

    settings = Settings(app_env="test", secrets_provider="env")
    provider = build_secrets_provider(settings, None)  # type: ignore[arg-type]
    assert provider.name == "env"


# --- Token generation ----------------------------------------------------


def test_tokens_are_unique_and_high_entropy() -> None:
    tokens = {new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(token) >= 40 for token in tokens)


def test_token_hash_is_deterministic_and_not_the_token() -> None:
    token = new_token()
    digest = session_token_hash(token)
    assert digest == session_token_hash(token)
    assert token not in digest
    assert len(digest) == 64


def test_csrf_hash_differs_from_session_hash() -> None:
    token = new_token()
    assert csrf_token_hash(token) != session_token_hash(token)


def test_content_hash_is_stable_and_order_independent() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_constant_time_comparison() -> None:
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
    assert constant_time_equals("abc", "abcd") is False
    assert constant_time_equals("", "") is True


# --- Redaction -----------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passphrase",
        "api_key",
        "apikey",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "authorization",
        "credential",
        "session_id",
        "cookie",
        "master_key",
        "connection_string",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    redacted = redact_value({"safe": "kept", key: "super-sensitive-value"})
    assert redacted[key] == REDACTED
    assert redacted["safe"] == "kept"
    assert "super-sensitive-value" not in str(redacted)


def test_sensitive_key_detection_is_case_insensitive() -> None:
    assert SENSITIVE_KEY_PATTERN.search("API_KEY")
    assert SENSITIVE_KEY_PATTERN.search("Password")
    assert SENSITIVE_KEY_PATTERN.search("X-Api-Key")


def test_non_sensitive_keys_are_kept() -> None:
    assert redact_value({"machine_id": "LINE-01", "value": "60.0"}) == {
        "machine_id": "LINE-01",
        "value": "60.0",
    }


def test_redaction_recurses_into_nested_structures() -> None:
    redacted = redact_value({"outer": {"inner": [{"token": "value"}, {"safe": "kept"}]}})
    assert redacted["outer"]["inner"][0]["token"] == REDACTED
    assert redacted["outer"]["inner"][1]["safe"] == "kept"


def test_redaction_terminates_on_deep_structures() -> None:
    deep: dict[str, object] = {"level": {}}
    cursor = deep["level"]
    for _ in range(40):
        cursor["level"] = {}  # type: ignore[index]
        cursor = cursor["level"]  # type: ignore[index]
    assert redact_value(deep) is not None


def test_redact_mapping_mutates_in_place() -> None:
    payload = {"token": "value", "machine_id": "LINE-01"}
    redact_mapping(payload)
    assert payload["token"] == REDACTED
    assert payload["machine_id"] == "LINE-01"


@pytest.mark.parametrize(
    "text_value",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefgh.ijklmnop",
        "api key sk-abcdefghijklmnopqrstuvwxyz",
        "github token ghp_abcdefghijklmnopqrst",
        "slack token xoxb-1234567890-abcdefghijkl",
        "gitlab token glpat-abcdefghijklmnopqrst",
    ],
)
def test_credential_shaped_substrings_are_scrubbed(text_value: str) -> None:
    assert REDACTED in redact_text(text_value)


def test_url_credentials_are_scrubbed() -> None:
    scrubbed = redact_text("postgresql://user:hunter2@db.internal:5432/app")
    assert "hunter2" not in scrubbed


def test_ordinary_text_is_unchanged() -> None:
    assert redact_text("Captured 500 rows from LINE-01") == "Captured 500 rows from LINE-01"


def test_connection_response_exposes_only_presence_of_credentials() -> None:
    """T4/T13: a client learns whether a credential exists, never its reference."""
    from datetime import UTC, datetime

    from humisense.api.routers.connections import _connection_response
    from humisense.db.models import Connection, ConnectionStatus

    organization_id = uuid.uuid4()
    connection = Connection(
        id=uuid.uuid4(),
        organization_id=organization_id,
        connector_type="csv",
        name="redaction-check",
        config={"path": "a.csv"},
        secret_ref=f"connection-{organization_id}",
        status=ConnectionStatus.ACTIVE,
        enabled=True,
        capabilities={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    payload = _connection_response(connection).model_dump()
    assert payload["has_credentials"] is True
    assert "secret_ref" not in payload
    assert "credentials" not in payload
    assert str(connection.secret_ref) not in str(payload)


def test_connector_context_redacts_credential_shaped_config() -> None:
    from humisense.connectors.types import ConnectorContext

    context = ConnectorContext(
        organization_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        connector_type="csv",
        correlation_id="redaction",
        config={"path": "a.csv", "api_key": "should-be-hidden"},
    )
    redacted = context.redacted_config()
    assert redacted["api_key"] == REDACTED
    assert redacted["path"] == "a.csv"
