"""Migration-environment tests.

Section 15: migrations are part of the deliverable, so the Alembic environment is tested
rather than assumed. These run Alembic in a subprocess so the real ``alembic/env.py`` and
``alembic.ini`` are exercised, not a re-implementation of them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

BACKEND_ROOT = Path(__file__).resolve().parent.parent
# A password with characters that configparser would treat as interpolation syntax.
ENCODED_PASSWORD_URL = "postgresql+asyncpg://humisense_app:p%40ss%25word@localhost:5432/humisense"


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": database_url}
    # Arguments are literals from this module and the interpreter is sys.executable; no
    # value reaches this call from a request or the environment. A subprocess is used
    # deliberately so the real alembic.ini and env.py run, not a stub.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_offline_migration_generation_survives_an_encoded_password() -> None:
    """Regression: the DSN was written into the Alembic Config, which interpolates '%'.

    A URL-encoded password raised ``TypeError: option values must be strings`` (a SecretStr
    was passed) and, once that was fixed, would still have been mangled by configparser.
    The environment now builds the engine from the unwrapped DSN instead.
    """
    result = _alembic("upgrade", "head", "--sql", database_url=ENCODED_PASSWORD_URL)

    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE" in result.stdout
    assert "InterpolationSyntaxError" not in result.stderr


def test_every_tenant_table_is_forced_under_row_level_security() -> None:
    """The generated DDL must enable AND force RLS; enabling alone lets the owner bypass."""
    result = _alembic("upgrade", "head", "--sql", database_url=ENCODED_PASSWORD_URL)
    assert result.returncode == 0, result.stderr

    sql = result.stdout
    from humisense.db.rls import RLS_TABLES

    for table in RLS_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY {table}_tenant_isolation" in sql


def test_audit_events_are_protected_from_modification_in_the_migration() -> None:
    """Section 11: audit records must resist ordinary update and delete at the database."""
    result = _alembic("upgrade", "head", "--sql", database_url=ENCODED_PASSWORD_URL)
    assert result.returncode == 0, result.stderr

    assert "audit_events_reject_mutation" in result.stdout
    assert "CREATE TRIGGER audit_events_immutable" in result.stdout
    assert "BEFORE UPDATE OR DELETE ON audit_events" in result.stdout


def test_the_migration_creates_every_expected_table() -> None:
    """A missing table is a broken deployment, so assert the full set."""
    result = _alembic("upgrade", "head", "--sql", database_url=ENCODED_PASSWORD_URL)
    assert result.returncode == 0, result.stderr

    for table in (
        "organizations",
        "users",
        "memberships",
        "connections",
        "sync_jobs",
        "source_records",
        "ingestion_receipts",
        "secrets",
        "sessions",
        "audit_events",
    ):
        assert f"CREATE TABLE {table}" in result.stdout, f"{table} missing from migration"
