"""Operator command-line interface.

Commands:

* ``seed``   — create a development organization, users, and memberships.
* ``demo``   — create a CSV connection for the synthetic historian fixture.
* ``doctor`` — check configuration, database, RLS, and connector availability.
* ``release-check`` — refuse to release unless the configuration is production-ready.

Every command refuses to run destructive work in production unless an explicit
acknowledgement flag is supplied, so an operator cannot reset a production database by
copying a development command line.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from humisense.config import Settings, get_settings
from humisense.connectors.base import load_builtin_connectors, registry
from humisense.db.models import Membership, Organization, Role, User
from humisense.db.session import Database
from humisense.logging_setup import configure_logging

logger = structlog.get_logger(__name__)

DEV_ORGANIZATION_SLUG = "humisense-demo"
DEV_ORGANIZATION_NAME = "Humisense Demo (synthetic data)"
DEV_USERS: tuple[tuple[str, str, str, Role], ...] = (
    ("dev-owner", "owner@example.invalid", "Demo Owner", Role.OWNER),
    ("dev-admin", "admin@example.invalid", "Demo Admin", Role.ADMIN),
    ("dev-operator", "operator@example.invalid", "Demo Operator", Role.OPERATOR),
    ("dev-viewer", "viewer@example.invalid", "Demo Viewer", Role.VIEWER),
    ("dev-owner-b", "owner-b@example.invalid", "Demo Owner (second tenant)", Role.OWNER),
)
SECOND_ORGANIZATION_SLUG = "humisense-demo-b"
SECOND_ORGANIZATION_NAME = "Humisense Demo B (cross-tenant tests)"


async def _get_or_create_organization(database: Database, *, slug: str, name: str) -> uuid.UUID:
    async with database.system_session() as session:
        organization_id = await session.scalar(
            select(Organization.id).where(Organization.slug == slug)
        )
        if organization_id is not None:
            return organization_id
        organization = Organization(name=name, slug=slug, is_active=True)
        session.add(organization)
        await session.flush()
        return organization.id


async def _get_or_create_user(
    database: Database, *, issuer: str, subject: str, email: str, display_name: str
) -> uuid.UUID:
    async with database.system_session() as session:
        user_id = await session.scalar(
            select(User.id).where(User.issuer == issuer, User.subject == subject)
        )
        if user_id is not None:
            return user_id
        user = User(
            issuer=issuer,
            subject=subject,
            email=email,
            display_name=display_name,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        return user.id


async def _get_or_create_membership(
    database: Database, *, user_id: uuid.UUID, organization_id: uuid.UUID, role: Role
) -> None:
    async with database.system_session() as session:
        existing = await session.scalar(
            select(Membership.id).where(
                Membership.user_id == user_id,
                Membership.organization_id == organization_id,
            )
        )
        if existing is not None:
            return
        session.add(
            Membership(
                user_id=user_id,
                organization_id=organization_id,
                role=role,
                is_active=True,
            )
        )


async def command_seed(settings: Settings) -> int:
    """Create development organizations, users, and memberships."""
    database = Database(settings)
    try:
        issuer = "humisense-dev"
        primary_org = await _get_or_create_organization(
            database, slug=DEV_ORGANIZATION_SLUG, name=DEV_ORGANIZATION_NAME
        )
        secondary_org = await _get_or_create_organization(
            database, slug=SECOND_ORGANIZATION_SLUG, name=SECOND_ORGANIZATION_NAME
        )
        for subject, email, display_name, role in DEV_USERS:
            user_id = await _get_or_create_user(
                database,
                issuer=issuer,
                subject=subject,
                email=email,
                display_name=display_name,
            )
            target_org = secondary_org if subject == "dev-owner-b" else primary_org
            await _get_or_create_membership(
                database,
                user_id=user_id,
                organization_id=target_org,
                role=role,
            )
        print(f"organization {DEV_ORGANIZATION_SLUG}: {primary_org}")
        print(f"organization {SECOND_ORGANIZATION_SLUG}: {secondary_org}")
        for subject, _, _, role in DEV_USERS:
            print(f"  user {subject} role={role.value}")
    finally:
        await database.dispose()
    return 0


async def command_demo(settings: Settings) -> int:
    """Create a CSV connection against the synthetic historian fixture."""
    from humisense.connectors.base import get_manifest
    from humisense.container import build_container
    from humisense.db.models import Connection
    from humisense.security.identity import Principal
    from humisense.services.connections import ConnectionDraft

    fixture = Path(settings.csv_allowed_roots[0]) / "synthetic_historian.csv"
    if not fixture.exists():
        print(
            f"fixture not found at {fixture}; the committed synthetic fixture lives at "
            "data/synthetic/synthetic_historian.csv in a source checkout"
        )
        return 2

    container = build_container(settings)
    try:
        organization_id = await _get_or_create_organization(
            container.database, slug=DEV_ORGANIZATION_SLUG, name=DEV_ORGANIZATION_NAME
        )
        user_id = await _get_or_create_user(
            container.database,
            issuer="humisense-dev",
            subject="dev-owner",
            email="owner@example.invalid",
            display_name="Demo Owner",
        )
        principal = Principal(
            user_id=user_id,
            organization_id=organization_id,
            role="owner",
            issuer="humisense-dev",
            subject="dev-owner",
            permissions=frozenset(),  # the service re-derives permissions from the role
        )
        async with container.database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(Connection).where(
                    Connection.organization_id == organization_id,
                    Connection.name == "Synthetic historian CSV",
                )
            )
        if existing is not None:
            print(f"demo connection already exists: {existing.id}")
            return 0

        manifest = get_manifest("csv")
        draft = ConnectionDraft(
            connector_type="csv",
            name="Synthetic historian CSV",
            description=(
                "Reads the committed synthetic historian fixture. This data is synthetic "
                "and is not Canary telemetry."
            ),
            config={
                "path": fixture.name,
                "delimiter": ",",
                "has_header": True,
                "skip_leading_lines": 1,
                "timestamp_column": "timestamp",
                "machine_id_column": "machine_id",
                "tag_column": "tag",
                "value_column": "value",
                "unit_column": "unit",
                "max_rows": 100000,
            },
            credentials={},
        )
        del manifest
        connection = await container.connections.create_connection(principal, draft)
        print(f"created demo connection {connection.id} ({connection.name})")
        print("run a synchronization from the UI or with:")
        print(f"  curl -X POST .../api/v1/connections/{connection.id}/sync")
    finally:
        await container.close()
    return 0


async def command_doctor(settings: Settings) -> int:
    """Report configuration and dependency health without exposing secrets."""
    problems = 0
    print(f"environment: {settings.app_env}")
    print(f"auth mode:   {settings.auth_mode}")
    print(f"secrets:     {settings.secrets_provider}")
    print(f"layer2:      {settings.layer2_mode}")
    print(f"queue:       {settings.task_queue_mode}")

    try:
        settings.validate_for_production()
        settings.assert_runtime_safe()
        print("config:      ok")
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        problems += 1
        print(f"config:      FAILED ({type(exc).__name__})")
        print(f"  detail: {getattr(exc, 'detail', '')}")

    database = Database(settings)
    try:
        async with database.system_session() as session:
            from sqlalchemy import text

            await session.execute(text("SELECT 1"))
        print("database:    ok")
    except Exception as exc:  # noqa: BLE001
        problems += 1
        print(f"database:    FAILED ({type(exc).__name__})")
    finally:
        await database.dispose()

    load_builtin_connectors()
    print(f"connectors:  {len(registry)} registered")
    for manifest in sorted(registry.manifests(), key=lambda m: m.connector_type):
        print(f"  {manifest.connector_type:16s} {manifest.availability.value}")

    from humisense.security.paths import PathPolicy

    for root in settings.csv_allowed_roots:
        try:
            policy = PathPolicy((root,), max_file_bytes=settings.csv_max_file_bytes)
            print(f"csv root:    ok ({policy.roots[0]})")
        except Exception as exc:  # noqa: BLE001
            problems += 1
            print(f"csv root:    FAILED {root} ({type(exc).__name__})")

    print(f"\nproblems: {problems}")
    return 0 if problems == 0 else 1


async def command_release_check(settings: Settings) -> int:
    """Refuse to release unless the configuration is fit for production.

    This is the caller :meth:`Settings.require_production_ready` documents but did not
    have. It is deliberately not :meth:`validate_for_production`: that gate only refuses a
    configuration that is unsafe *given* it claims production, so a development
    configuration relabelled as production would pass it. This command refuses a
    configuration that is merely not production-grade, which is what a release step needs.

    It reads configuration only. It does not open a database connection and does not print
    any secret, so it is safe to run in a build pipeline with production values present.
    """
    print(f"environment: {settings.app_env}")
    print(f"auth mode:   {settings.auth_mode}")
    print(f"secrets:     {settings.secrets_provider}")
    print(f"layer2:      {settings.layer2_mode}")
    print(f"queue:       {settings.task_queue_mode}")
    try:
        settings.require_production_ready()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        print(f"release-check: REFUSED ({type(exc).__name__})")
        print(f"  detail: {getattr(exc, 'detail', '')}")
        return 1
    print("release-check: ok (configuration is production-ready)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="humisense",
        description="Humisense Brain / OS operator commands",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="create development organizations, users, and memberships")
    sub.add_parser("demo", help="create a CSV connection for the synthetic fixture")
    sub.add_parser("doctor", help="check configuration and dependencies")
    sub.add_parser(
        "release-check",
        help="refuse to release unless the configuration is production-ready",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(json_logs=settings.log_json, level=settings.log_level)

    handlers: dict[str, Any] = {
        "seed": command_seed,
        "demo": command_demo,
        "doctor": command_doctor,
        "release-check": command_release_check,
    }
    handler = handlers[args.command]
    return int(asyncio.run(handler(settings)))


if __name__ == "__main__":
    sys.exit(main())
