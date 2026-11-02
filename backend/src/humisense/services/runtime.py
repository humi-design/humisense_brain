"""Connector runtime construction.

The runtime is the boundary between the platform and a connector implementation. It
builds a :class:`ConnectorContext` containing only what the connector is entitled to:

* the tenant's non-secret configuration for that connection;
* the credentials resolved from the secrets provider for that connection;
* resource limits derived from server configuration;
* an egress policy, for connectors that make network calls;
* a path policy, for connectors that read the filesystem;
* a cancellation token.

It deliberately does **not** provide a database session, the tenant model, or any other
connection's configuration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from humisense.config import Settings
from humisense.connectors.base import BaseConnector, build_connector
from humisense.connectors.types import (
    CancellationToken,
    ConnectorContext,
    ResourceLimits,
)
from humisense.db.models import Connection
from humisense.errors import ConnectorConfigError
from humisense.secrets.base import SecretRef, SecretsProvider
from humisense.security.egress import EgressPolicy
from humisense.security.paths import PathPolicy

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ConnectorRuntime:
    """Builds connector instances for one platform configuration."""

    settings: Settings
    secrets_provider: SecretsProvider
    _path_policy: PathPolicy | None = None

    def path_policy(self) -> PathPolicy | None:
        """Return the filesystem policy, or None when no roots are configured."""
        if self._path_policy is None:
            if not self.settings.csv_allowed_roots:
                return None
            try:
                self._path_policy = PathPolicy(
                    self.settings.csv_allowed_roots,
                    max_file_bytes=self.settings.csv_max_file_bytes,
                )
            except Exception as exc:  # noqa: BLE001 - misconfiguration is reported, not raised
                logger.error(
                    "path_policy_unavailable",
                    error_type=type(exc).__name__,
                    note="file connectors will fail closed",
                )
                return None
        return self._path_policy

    def egress_policy(self, *, extra_allowed_hosts: tuple[str, ...] = ()) -> EgressPolicy:
        return EgressPolicy(self.settings, extra_allowed_hosts=extra_allowed_hosts)

    def resource_limits(self) -> ResourceLimits:
        return ResourceLimits(
            max_records=self.settings.csv_max_rows,
            max_file_bytes=self.settings.csv_max_file_bytes,
            max_processing_seconds=self.settings.csv_max_processing_seconds,
            page_size=self.settings.sync_batch_size,
        )

    async def resolve_credentials(self, connection: Connection) -> dict[str, Any]:
        """Resolve this connection's credentials, or return an empty mapping.

        A connection with no ``secret_ref`` legitimately has no credentials (the CSV
        connector reads from an already-authorized location).
        """
        if not connection.secret_ref:
            return {}
        ref = SecretRef(name=connection.secret_ref, organization_id=connection.organization_id)
        return await self.secrets_provider.get(ref)

    async def build_context(
        self,
        connection: Connection,
        *,
        correlation_id: str,
        cancellation: CancellationToken | None = None,
    ) -> ConnectorContext:
        """Assemble the execution context for one connector operation."""
        credentials = await self.resolve_credentials(connection)
        context = ConnectorContext(
            organization_id=connection.organization_id,
            connection_id=connection.id,
            connector_type=connection.connector_type,
            correlation_id=correlation_id,
            config=dict(connection.config or {}),
            credentials=credentials,
            limits=self.resource_limits(),
            cancellation=cancellation or _never_cancelled(),
            egress_policy=self.egress_policy(),
            path_policy=self.path_policy(),
        )
        return context

    async def build_connector(
        self,
        connection: Connection,
        *,
        correlation_id: str,
        cancellation: CancellationToken | None = None,
    ) -> BaseConnector:
        context = await self.build_context(
            connection, correlation_id=correlation_id, cancellation=cancellation
        )
        return build_connector(connection.connector_type, context)


class _NeverCancelled:
    __slots__ = ()

    @property
    def cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


_NEVER_CANCELLED = _NeverCancelled()


def _never_cancelled() -> CancellationToken:
    return _NEVER_CANCELLED


def secret_ref_name(connection_id: uuid.UUID) -> str:
    """Return the stable secret name for a connection's credentials."""
    return f"connection-{connection_id}"


async def store_connection_credentials(
    provider: SecretsProvider,
    *,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    credentials: dict[str, Any],
    created_by: uuid.UUID | None = None,
) -> str:
    """Write credentials and return the reference name stored on the connection."""
    name = secret_ref_name(connection_id)
    ref = SecretRef(name=name, organization_id=organization_id)
    await provider.put(ref, credentials, created_by=created_by)
    return name


async def revoke_connection_credentials(
    provider: SecretsProvider,
    *,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> bool:
    ref = SecretRef(name=secret_ref_name(connection_id), organization_id=organization_id)
    try:
        return await provider.delete(ref)
    except Exception as exc:  # noqa: BLE001 - provider may be read-only
        logger.warning(
            "credential_revocation_failed",
            organization_id=str(organization_id),
            error_type=type(exc).__name__,
        )
        return False


def split_config_and_secrets(
    payload: dict[str, Any], secret_field_names: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a submitted configuration into non-secret config and credential material.

    The split is driven by the connector manifest, not by the request, so a caller cannot
    influence which fields are treated as secrets.
    """
    if not secret_field_names:
        return dict(payload), {}
    config: dict[str, Any] = {}
    secrets: dict[str, Any] = {}
    for key, value in payload.items():
        if key in secret_field_names:
            secrets[key] = value
        else:
            config[key] = value
    return config, secrets


def validate_connector_config(connector: BaseConnector, config: dict[str, Any]) -> None:
    """Run connector-side validation and translate failures into a typed error."""
    try:
        connector.validate_config(config)
    except ConnectorConfigError:
        raise
    except Exception as exc:
        logger.error("connector_config_validation_crashed", error_type=type(exc).__name__)
        raise ConnectorConfigError(
            "The connector could not validate this configuration.",
            detail="connector.validate_config raised an unexpected exception",
        ) from exc
