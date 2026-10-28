"""Alembic environment.

The database URL and the connection come from application settings, so migrations use the
same restricted role as the application. That role is the table owner, and every
tenant-owned table uses FORCE ROW LEVEL SECURITY, so owning a table does not let the
application bypass its policies.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from humisense.config import get_settings
from humisense.db import models  # noqa: F401  (import registers the tables)
from humisense.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
settings = get_settings()


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # Build the engine from the unwrapped DSN rather than writing it into the alembic
    # Config: configparser treats '%' as interpolation syntax, so a URL-encoded password
    # would either fail or be silently mangled.
    connectable = create_async_engine(settings.database_dsn, poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
