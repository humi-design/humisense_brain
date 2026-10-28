"""Connection lifecycle service.

Every operation takes the caller's :class:`Principal` and re-derives the tenant from it.
A connection is always looked up by ``(organization_id, connection_id)`` so a foreign
identifier produces "not found" rather than a permission error, which avoids confirming
that another tenant's object exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from humisense.config import Settings
from humisense.connectors.base import ensure_available, get_manifest
from humisense.db.models import Connection, ConnectionStatus
from humisense.db.session import Database
from humisense.errors import (
    AuthorizationError,
    ConflictError,
    ConnectorUnavailableError,
    NotFoundError,
    ValidationFailure,
)
from humisense.security.identity import Principal
from humisense.security.permissions import Permission, role_has_permission
from humisense.services.audit import (
    OUTCOME_SUCCESS,
    AuditAction,
    AuditActor,
    AuditEntry,
    AuditService,
)
from humisense.services.runtime import (
    ConnectorRuntime,
    revoke_connection_credentials,
    secret_ref_name,
    store_connection_credentials,
)

logger = structlog.get_logger(__name__)

MAX_CONNECTIONS_PER_ORGANIZATION = 200


@dataclass(slots=True)
class ConnectionDraft:
    """Validated input for creating or updating a connection."""

    connector_type: str
    name: str
    description: str | None
    config: dict[str, Any]
    credentials: dict[str, Any]


class ConnectionService:
    """Creates, reads, updates, and deletes tenant-owned connections."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        audit: AuditService,
        runtime: ConnectorRuntime,
    ) -> None:
        self._database = database
        self._settings = settings
        self._audit = audit
        self._runtime = runtime

    # --- Authorization helpers -------------------------------------------

    def _require(self, principal: Principal, permission: Permission) -> None:
        if not role_has_permission(principal.role, permission):
            raise AuthorizationError(
                detail="Role does not grant the required permission",
                context={"role": principal.role, "permission": permission.value},
            )

    async def _load_owned(
        self, principal: Principal, connection_id: uuid.UUID, session: AsyncSession
    ) -> Connection:
        """Load a connection scoped to the caller's tenant, or raise NotFound."""
        connection = await session.scalar(
            select(Connection).where(
                Connection.organization_id == principal.organization_id,
                Connection.id == connection_id,
            )
        )
        if connection is None:
            raise NotFoundError(detail="Connection not found for this organization")
        return connection

    # --- Reads ------------------------------------------------------------

    async def list_connections(
        self,
        principal: Principal,
        *,
        limit: int,
        offset: int = 0,
        status: ConnectionStatus | None = None,
    ) -> tuple[list[Connection], int]:
        self._require(principal, Permission.CONNECTIONS_READ)
        async with self._database.tenant_session(principal.organization_id) as session:
            statement = select(Connection).where(
                Connection.organization_id == principal.organization_id
            )
            count_statement = (
                select(func.count())
                .select_from(Connection)
                .where(Connection.organization_id == principal.organization_id)
            )
            if status is not None:
                statement = statement.where(Connection.status == status)
                count_statement = count_statement.where(Connection.status == status)
            statement = statement.order_by(Connection.created_at.desc()).limit(limit).offset(offset)
            rows = list((await session.scalars(statement)).all())
            total = int(await session.scalar(count_statement) or 0)
        return rows, total

    async def get_connection(self, principal: Principal, connection_id: uuid.UUID) -> Connection:
        self._require(principal, Permission.CONNECTIONS_READ)
        async with self._database.tenant_session(principal.organization_id) as session:
            return await self._load_owned(principal, connection_id, session)

    async def get_connection_for_worker(
        self, organization_id: uuid.UUID, connection_id: uuid.UUID
    ) -> Connection:
        """Load a connection inside the worker, with the tenant scope re-applied.

        The worker is not a user; it re-establishes tenant context from the job record
        rather than trusting anything carried in the job payload beyond the two ids.
        """
        async with self._database.tenant_session(organization_id) as session:
            connection = await session.scalar(
                select(Connection).where(
                    Connection.organization_id == organization_id,
                    Connection.id == connection_id,
                )
            )
            if connection is None:
                raise NotFoundError(detail="Connection not found for this organization")
            return connection

    # --- Writes -----------------------------------------------------------

    async def create_connection(self, principal: Principal, draft: ConnectionDraft) -> Connection:
        self._require(principal, Permission.CONNECTIONS_CREATE)

        manifest = self._manifest_for(draft.connector_type)
        config, credentials = self._split_and_validate(draft, manifest)

        connection_id = uuid.uuid4()
        secret_ref: str | None = None
        if credentials:
            secret_ref = await store_connection_credentials(
                self._runtime.secrets_provider,
                organization_id=principal.organization_id,
                connection_id=connection_id,
                credentials=credentials,
                created_by=principal.user_id,
            )

        try:
            async with self._database.tenant_session(principal.organization_id) as session:
                existing = await session.scalar(
                    select(func.count())
                    .select_from(Connection)
                    .where(Connection.organization_id == principal.organization_id)
                )
                if int(existing or 0) >= MAX_CONNECTIONS_PER_ORGANIZATION:
                    raise ValidationFailure(
                        detail="Organization has reached the connection limit",
                        context={"limit": MAX_CONNECTIONS_PER_ORGANIZATION},
                    )

                connection = Connection(
                    id=connection_id,
                    organization_id=principal.organization_id,
                    connector_type=draft.connector_type,
                    name=draft.name,
                    description=draft.description,
                    config=config,
                    secret_ref=secret_ref,
                    status=ConnectionStatus.PENDING,
                    enabled=True,
                    capabilities=manifest.capabilities.as_dict(),
                    created_by=principal.user_id,
                )
                session.add(connection)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise ConflictError(
                        detail="A connection with this name already exists in the organization"
                    ) from exc
                await session.refresh(connection)
        except Exception:
            if secret_ref:
                await revoke_connection_credentials(
                    self._runtime.secrets_provider,
                    organization_id=principal.organization_id,
                    connection_id=connection_id,
                )
            raise

        await self._audit.record(
            AuditActor.user(principal),
            AuditEntry(
                action=AuditAction.CONNECTION_CREATED,
                outcome=OUTCOME_SUCCESS,
                target_type="connection",
                target_id=str(connection_id),
                metadata={
                    "connector_type": draft.connector_type,
                    "credentials_stored": bool(secret_ref),
                },
            ),
        )
        logger.info(
            "connection_created",
            connection_id=str(connection_id),
            connector_type=draft.connector_type,
            organization_id=str(principal.organization_id),
        )
        return connection

    async def update_connection(
        self,
        principal: Principal,
        connection_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        config: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> Connection:
        self._require(principal, Permission.CONNECTIONS_UPDATE)

        async with self._database.tenant_session(principal.organization_id) as session:
            connection = await self._load_owned(principal, connection_id, session)
            connector_type = connection.connector_type

            if config is not None:
                # Validate against the connector before persisting. The connector type is
                # taken from the stored row, never from the request, so a caller cannot
                # change the type of an existing connection.
                candidate = dict(connection.config or {})
                candidate.update(config)
                self._validate_config(connector_type, candidate)
                connection.config = candidate

            if name is not None:
                connection.name = name
            if description is not None:
                connection.description = description
            if enabled is not None:
                connection.enabled = enabled
                if not enabled:
                    connection.status = ConnectionStatus.DISABLED
                elif connection.status == ConnectionStatus.DISABLED:
                    connection.status = ConnectionStatus.PENDING

            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConflictError(
                    detail="A connection with this name already exists in the organization"
                ) from exc
            await session.refresh(connection)

        if credentials:
            await store_connection_credentials(
                self._runtime.secrets_provider,
                organization_id=principal.organization_id,
                connection_id=connection_id,
                credentials=credentials,
                created_by=principal.user_id,
            )
            async with self._database.tenant_session(principal.organization_id) as session:
                stored = await self._load_owned(principal, connection_id, session)
                stored.secret_ref = secret_ref_name(connection_id)
                await session.flush()

        await self._audit.record(
            AuditActor.user(principal),
            AuditEntry(
                action=AuditAction.CONNECTION_UPDATED,
                outcome=OUTCOME_SUCCESS,
                target_type="connection",
                target_id=str(connection_id),
                metadata={
                    "config_changed": config is not None,
                    "credentials_replaced": bool(credentials),
                    "enabled": enabled,
                },
            ),
        )
        return await self.get_connection(principal, connection_id)

    async def set_enabled(
        self, principal: Principal, connection_id: uuid.UUID, *, enabled: bool
    ) -> Connection:
        self._require(principal, Permission.CONNECTIONS_UPDATE)
        async with self._database.tenant_session(principal.organization_id) as session:
            connection = await self._load_owned(principal, connection_id, session)
            connection.enabled = enabled
            connection.status = ConnectionStatus.PENDING if enabled else ConnectionStatus.DISABLED
            await session.flush()
        await self._audit.record(
            AuditActor.user(principal),
            AuditEntry(
                action=(
                    AuditAction.CONNECTION_ENABLED if enabled else AuditAction.CONNECTION_DISABLED
                ),
                outcome=OUTCOME_SUCCESS,
                target_type="connection",
                target_id=str(connection_id),
            ),
        )
        return await self.get_connection(principal, connection_id)

    async def delete_connection(self, principal: Principal, connection_id: uuid.UUID) -> None:
        """Delete a connection, its credentials, and its captured records.

        Credentials are revoked before the row is deleted, because after deletion the
        secret's reference is gone and it would become unreachable.
        """
        self._require(principal, Permission.CONNECTIONS_DELETE)
        async with self._database.tenant_session(principal.organization_id) as session:
            connection = await self._load_owned(principal, connection_id, session)
            await session.delete(connection)

        await revoke_connection_credentials(
            self._runtime.secrets_provider,
            organization_id=principal.organization_id,
            connection_id=connection_id,
        )
        await self._audit.record(
            AuditActor.user(principal),
            AuditEntry(
                action=AuditAction.CONNECTION_DELETED,
                outcome=OUTCOME_SUCCESS,
                target_type="connection",
                target_id=str(connection_id),
                metadata={"records_cascaded": True},
            ),
        )
        logger.info(
            "connection_deleted",
            connection_id=str(connection_id),
            organization_id=str(principal.organization_id),
        )

    # --- Status -----------------------------------------------------------

    async def record_test_result(
        self,
        organization_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        ok: bool,
        error_code: str | None,
        actor: AuditActor | None = None,
    ) -> None:
        """Persist the outcome of a connection test."""
        async with self._database.tenant_session(organization_id) as session:
            connection = await session.scalar(
                select(Connection).where(
                    Connection.organization_id == organization_id,
                    Connection.id == connection_id,
                )
            )
            if connection is None:
                return
            connection.last_tested_at = datetime.now(UTC)
            if ok:
                connection.status = ConnectionStatus.ACTIVE
                connection.last_success_at = datetime.now(UTC)
                connection.last_error_code = None
            else:
                connection.status = (
                    ConnectionStatus.ERROR if connection.enabled else ConnectionStatus.DISABLED
                )
                connection.last_error_code = error_code
            await session.flush()

        if actor is not None:
            await self._audit.record(
                actor,
                AuditEntry(
                    action=AuditAction.CONNECTION_TESTED,
                    outcome=OUTCOME_SUCCESS if ok else "failure",
                    target_type="connection",
                    target_id=str(connection_id),
                    metadata={"ok": ok, "error_code": error_code},
                ),
            )

    async def record_sync_outcome(
        self,
        organization_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        ok: bool,
        error_code: str | None,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        """Update connection state after a synchronization attempt."""
        async with self._database.tenant_session(organization_id) as session:
            connection = await session.scalar(
                select(Connection).where(
                    Connection.organization_id == organization_id,
                    Connection.id == connection_id,
                )
            )
            if connection is None:
                return
            if ok:
                connection.last_success_at = datetime.now(UTC)
                connection.status = ConnectionStatus.ACTIVE
                connection.last_error_code = None
                if checkpoint is not None:
                    connection.checkpoint = checkpoint
            else:
                connection.status = ConnectionStatus.ERROR
                connection.last_error_code = error_code
            await session.flush()

    # --- Internals --------------------------------------------------------

    def _manifest_for(self, connector_type: str) -> Any:
        try:
            return ensure_available(connector_type)
        except ConnectorUnavailableError:
            raise

    def _secret_field_names(self, connector_type: str) -> set[str]:
        manifest = get_manifest(connector_type)
        return {f.name for f in manifest.config_fields if f.is_secret}

    def _split_and_validate(
        self, draft: ConnectionDraft, manifest: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate configuration and separate credential material from plain config.

        Secret fields are declared by the connector manifest. They are removed from the
        configuration so they can never be persisted in the JSONB config column, and are
        returned separately for storage in the secrets provider.
        """
        from humisense.services.runtime import split_config_and_secrets

        secret_fields = {f.name for f in manifest.config_fields if f.is_secret}
        allowed_fields = {f.name for f in manifest.config_fields}
        unknown = set(draft.config) - allowed_fields
        if unknown:
            raise ValidationFailure(
                detail="Configuration contains fields this connector does not declare",
                context={"unknown_fields": sorted(unknown)},
            )
        if draft.credentials and not secret_fields:
            raise ValidationFailure(
                detail="This connector does not accept credentials",
            )

        config, inline_secrets = split_config_and_secrets(draft.config, secret_fields)
        credentials = {**inline_secrets, **draft.credentials}

        self._validate_config(draft.connector_type, config)
        return config, credentials

    def _validate_config(self, connector_type: str, config: dict[str, Any]) -> None:
        manifest = get_manifest(connector_type)
        allowed_fields = {f.name for f in manifest.config_fields}
        unknown = set(config) - allowed_fields
        if unknown:
            raise ValidationFailure(
                detail="Configuration contains fields this connector does not declare",
                context={"unknown_fields": sorted(unknown)},
            )
        required = {f.name for f in manifest.config_fields if f.required and not f.is_secret}
        missing = sorted(name for name in required if config.get(name) in (None, ""))
        if missing:
            raise ValidationFailure(
                detail="Configuration is missing required fields",
                context={"missing_fields": missing},
            )
        from humisense.connectors.base import build_connector
        from humisense.connectors.types import ConnectorContext

        probe_context = ConnectorContext(
            organization_id=uuid.UUID(int=0),
            connection_id=uuid.UUID(int=0),
            connector_type=connector_type,
            correlation_id="validation",
            config=config,
            path_policy=self._runtime.path_policy(),
        )
        connector = build_connector(connector_type, probe_context)
        connector.validate_config(config)
