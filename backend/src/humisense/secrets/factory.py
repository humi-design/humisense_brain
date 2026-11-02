"""Secrets provider selection."""

from __future__ import annotations

from humisense.config import Settings
from humisense.db.session import Database
from humisense.errors import ConfigurationError
from humisense.secrets.base import SecretsProvider
from humisense.secrets.encrypted_db import EncryptedDbSecretsProvider
from humisense.secrets.env_provider import EnvSecretsProvider


def build_secrets_provider(settings: Settings, database: Database) -> SecretsProvider:
    """Return the configured secrets provider.

    Production deployments are expected to substitute a vault-backed implementation of
    the same protocol; nothing above this factory depends on the concrete type.
    """
    if settings.secrets_provider == "env":
        return EnvSecretsProvider(prefix=settings.secrets_env_prefix)
    if settings.secrets_provider == "encrypted_db":
        return EncryptedDbSecretsProvider(database, master_key=settings.secrets_master_key_value)
    raise ConfigurationError(
        detail=f"Unknown secrets provider {settings.secrets_provider!r}",
    )
