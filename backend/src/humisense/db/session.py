"""Async engine and tenant-scoped session management.

Tenant context is established with ``set_config('app.current_org', $1, true)``, which is
**transaction-local**. It is never set on a pooled connection in a way that could leak to
another request, and it always comes from a value the server derived from a verified
token rather than from a client-supplied parameter.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from humisense.config import Settings
from humisense.errors import ConfigurationError

TENANT_GUC = "app.current_org"
STATEMENT_TIMEOUT_GUC = "statement_timeout"


class Database:
    """Owns the async engine and produces correctly scoped sessions."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_async_engine(
            settings.database_dsn,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,
            # Server-side prepared statement caching is left at the asyncpg default;
            # it is safe here because we never use PgBouncer in transaction mode.
            echo=False,
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @asynccontextmanager
    async def tenant_session(self, organization_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """Yield a session whose transaction is scoped to one organization.

        The GUC is set inside the transaction, so it cannot leak across a pooled
        connection. On exit the transaction is committed on success and rolled back on
        any exception.
        """
        if organization_id is None:
            raise ConfigurationError(
                detail="tenant_session requires an organization id; refusing to run unscoped"
            )
        async with self._session_factory() as session, session.begin():
            await self._apply_transaction_settings(session, organization_id)
            yield session

    @asynccontextmanager
    async def system_session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session with no tenant context.

        Used only for identity and registry operations that must run before a tenant is
        known: resolving a user by ``(issuer, subject)``, session lookup by token hash,
        and membership discovery during sign-in. Row-Level Security tables have no
        policy that grants access without tenant context, so this session cannot read
        tenant data even if a query is written incorrectly.
        """
        async with self._session_factory() as session, session.begin():
            await self._apply_transaction_settings(session, None)
            yield session

    async def _apply_transaction_settings(
        self, session: AsyncSession, organization_id: uuid.UUID | None
    ) -> None:
        timeout_ms = self._settings.database_statement_timeout_ms
        await session.execute(
            text(f"SELECT set_config('{STATEMENT_TIMEOUT_GUC}', :timeout, true)"),
            {"timeout": str(timeout_ms)},
        )
        await session.execute(
            text(f"SELECT set_config('{TENANT_GUC}', :org, true)"),
            {"org": str(organization_id) if organization_id else ""},
        )

    async def dispose(self) -> None:
        await self._engine.dispose()
