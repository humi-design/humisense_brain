"""Shared test fixtures.

The suite runs against a real PostgreSQL database and a real (fakeredis-free) in-process
queue, because the security properties under test — Row-Level Security, unique-constraint
idempotency, and append-only audit triggers — do not exist in SQLite.

Each test function gets a freshly truncated database. Truncation runs as the restricted
application role, so the tests exercise the same privilege level as production.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from humisense.config import Settings
from humisense.container import Container, build_container
from humisense.db.models import Membership, Organization, Role, User
from humisense.security.identity import Principal
from humisense.security.permissions import permissions_for_role

TEST_DATABASE_URL = os.environ.get(
    "HUMISENSE_TEST_DATABASE_URL",
    "postgresql+asyncpg://humisense_app:humisense_app_dev_pw@localhost:5432/humisense_test",
)
TEST_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

TABLES_IN_TRUNCATION_ORDER = (
    "ingestion_receipts",
    "source_records",
    "sync_jobs",
    "connections",
    "secrets",
    "audit_events",
    "sessions",
    "memberships",
    "users",
    "organizations",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "security: security acceptance test")


@pytest.fixture(scope="session", autouse=True)
def load_connectors() -> None:
    """Register the built-in connectors once per test session.

    Registration is normally triggered by container startup. Tests that exercise the
    registry directly need it loaded without building a container.
    """
    from humisense.connectors.base import load_builtin_connectors

    load_builtin_connectors()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database() -> None:
    """Apply the migrations to the test database once per session.

    The suite truncates tables between tests but does not create them, so the schema has
    to exist before the first ``container`` fixture runs. Using the real migration
    environment keeps the tests exercising the same DDL that production runs, including
    the Row-Level Security policies, instead of a ``create_all`` approximation.
    """
    from alembic import command
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parent.parent
    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    alembic_config.attributes["database_dsn"] = TEST_DATABASE_URL
    command.upgrade(alembic_config, "head")


@pytest.fixture
def test_settings(tmp_path: Path) -> Iterator[Settings]:
    """Return settings pointing at the test database and a temporary CSV root."""
    csv_root = tmp_path / "csv"
    csv_root.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        app_env="test",
        log_json=False,
        log_level="WARNING",
        database_url=TEST_DATABASE_URL,
        database_pool_size=2,
        database_max_overflow=2,
        auth_mode="dev",
        session_secret="test-session-secret-that-is-long-enough-0000",
        secrets_provider="encrypted_db",
        secrets_master_key=TEST_MASTER_KEY,
        csv_allowed_roots=(str(csv_root),),
        csv_max_file_bytes=1_000_000,
        csv_max_rows=1000,
        csv_max_processing_seconds=10.0,
        layer2_mode="local_test_receiver",
        task_queue_mode="inline",
        rate_limit_enabled=True,
        cors_origins=("http://localhost:5173",),
        docs_enabled=True,
        metrics_enabled=False,
    )
    yield settings


@pytest_asyncio.fixture
async def container(test_settings: Settings) -> AsyncIterator[Container]:
    """Build a container against the test database and truncate all tables."""
    built = build_container(test_settings)
    async with built.database.system_session() as session:
        await session.execute(
            text(f"TRUNCATE {', '.join(TABLES_IN_TRUNCATION_ORDER)} RESTART IDENTITY CASCADE")
        )
    try:
        yield built
    finally:
        await built.close()


@pytest.fixture
def csv_root(tmp_path: Path) -> Path:
    """The temporary directory that acts as the allowed CSV root.

    This is the same directory ``test_settings`` allowlists, so a test can write a file
    and have the connector permitted to read it without patching the policy afterwards.
    """
    root = tmp_path / "csv"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest_asyncio.fixture
async def organization(container: Container) -> uuid.UUID:
    """Create one organization."""
    async with container.database.system_session() as session:
        record = Organization(name="Tenant A", slug=f"tenant-a-{uuid.uuid4().hex[:8]}")
        session.add(record)
        await session.flush()
        return record.id


@pytest_asyncio.fixture
async def other_organization(container: Container) -> uuid.UUID:
    """Create a second organization, for cross-tenant tests."""
    async with container.database.system_session() as session:
        record = Organization(name="Tenant B", slug=f"tenant-b-{uuid.uuid4().hex[:8]}")
        session.add(record)
        await session.flush()
        return record.id


async def make_principal(
    container: Container,
    organization_id: uuid.UUID,
    *,
    role: str = "owner",
    subject: str | None = None,
) -> Principal:
    """Create a user with a membership and return a principal for it."""
    resolved_subject = subject or f"user-{uuid.uuid4().hex[:10]}"
    async with container.database.system_session() as session:
        user = User(
            issuer="humisense-dev",
            subject=resolved_subject,
            email=f"{resolved_subject}@example.invalid",
            display_name=resolved_subject,
        )
        session.add(user)
        await session.flush()
        session.add(
            Membership(
                user_id=user.id,
                organization_id=organization_id,
                role=Role(role),
                is_active=True,
            )
        )
        user_id = user.id
    return Principal(
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        issuer="humisense-dev",
        subject=resolved_subject,
        email=f"{resolved_subject}@example.invalid",
        display_name=resolved_subject,
        permissions=permissions_for_role(role),
    )


@pytest_asyncio.fixture
async def owner(container: Container, organization: uuid.UUID) -> Principal:
    return await make_principal(container, organization, role="owner", subject="owner")


@pytest_asyncio.fixture
async def viewer(container: Container, organization: uuid.UUID) -> Principal:
    return await make_principal(container, organization, role="viewer", subject="viewer")


@pytest_asyncio.fixture
async def foreign_owner(container: Container, other_organization: uuid.UUID) -> Principal:
    return await make_principal(
        container, other_organization, role="owner", subject="foreign-owner"
    )


def write_csv(path: Path, rows: list[list[str]], *, header: bool = True, notice: str = "") -> Path:
    """Write a CSV fixture file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if notice:
            handle.write(f"{notice}\n")
        if header:
            handle.write("timestamp,machine_id,tag,value,unit\n")
        for row in rows:
            handle.write(",".join(row) + "\n")
    return path


def synthetic_rows(count: int = 10) -> list[list[str]]:
    """Return deterministic rows with the historian column shape."""
    return [
        [
            f"2026-01-01T00:{index:02d}:00Z",
            f"LINE-{index % 3 + 1:02d}",
            "temperature",
            f"{60 + index * 0.5:.3f}",
            "degC",
        ]
        for index in range(count)
    ]


async def drain_queue(container: Container) -> None:
    """Wait for inline-queued jobs to finish."""
    queue = container.task_queue
    drain = getattr(queue, "drain", None)
    if drain is not None:
        await drain(deadline_seconds=30)


async def run_pending_syncs(container: Container) -> None:
    """Drain the inline queue and assert no job raised unexpectedly."""
    await drain_queue(container)
    failures = getattr(container.task_queue, "failures", [])
    if failures:
        raise AssertionError(f"inline queue jobs failed: {failures!r}")


def utc_now() -> datetime:
    return datetime.now(UTC)


def minutes_ago(minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


@pytest.fixture
def asyncio_sleep() -> Any:
    return asyncio.sleep


@pytest_asyncio.fixture
async def app_client(
    container: Container, test_settings: Settings, csv_root: Path
) -> AsyncIterator[Any]:
    """An HTTP client bound to the real application, sharing the test container.

    The client drives the assembled ASGI application, so middleware, dependency wiring,
    and exception handlers are all exercised rather than bypassed.
    """
    from httpx import ASGITransport, AsyncClient

    from humisense.app import create_app

    test_settings.csv_allowed_roots = (str(csv_root),)
    container.settings.csv_allowed_roots = (str(csv_root),)
    container.runtime._path_policy = None

    app = create_app(test_settings, container=container)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        # ASGITransport does not run the lifespan, so attach the container directly.
        app.state.container = container
        yield client


async def sign_in(client: Any, *, subject: str, organization_slug: str) -> dict[str, Any]:
    """Create a session through the real sign-in route and return its body."""
    response = await client.post(
        "/api/v1/auth/session",
        json={"subject": subject, "organization_slug": organization_slug},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def organization_slug(container: Container, organization: uuid.UUID) -> str:
    """Return the slug the sign-in route expects for a tenant."""
    async with container.database.system_session() as session:
        return str(
            await session.scalar(
                text("SELECT slug FROM organizations WHERE id = :id"), {"id": organization}
            )
        )
