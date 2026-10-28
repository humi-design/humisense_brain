"""Row-Level Security DDL.

RLS is defence in depth. Application-level authorization is the primary control; these
policies exist so that a single forgotten ``WHERE organization_id = ...`` cannot become a
cross-tenant data breach.

Two details make the policies effective:

* ``FORCE ROW LEVEL SECURITY`` is applied as well as ``ENABLE``. Without ``FORCE``, the
  table owner (the application role) would bypass every policy.
* ``app_current_org()`` reads a transaction-local GUC, so it is empty for connections
  that have no tenant context and the policies deny by default.
"""

from __future__ import annotations

RLS_TABLES: tuple[str, ...] = (
    "connections",
    "sync_jobs",
    "source_records",
    "ingestion_receipts",
    "secrets",
    "audit_events",
)
"""Tables that hold tenant-owned data and carry a NOT NULL organization_id."""

CREATE_HELPER_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_org() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('app.current_org', true), '')::uuid
$$;
"""

DROP_HELPER_FUNCTION = "DROP FUNCTION IF EXISTS app_current_org();"


def enable_policy_sql(table: str) -> list[str]:
    """Return the statements that enforce tenant isolation on ``table``.

    The policy is written so that a NULL ``app_current_org()`` (no tenant context) never
    matches a row, which makes the default deny.
    """
    policy = f"{table}_tenant_isolation"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS {policy} ON {table};",
        (
            f"CREATE POLICY {policy} ON {table} "
            "USING (organization_id = app_current_org()) "
            "WITH CHECK (organization_id = app_current_org());"
        ),
    ]


def disable_policy_sql(table: str) -> list[str]:
    return [
        f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;",
    ]


# Statements are issued one at a time: the asyncpg driver used by the application does
# not accept a multi-statement script through a prepared statement.
AUDIT_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION audit_events_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_events is append-only; % is not permitted', TG_OP
            USING ERRCODE = '42501';
    END;
    $$;
    """,
    "DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events;",
    """
    CREATE TRIGGER audit_events_immutable
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_reject_mutation();
    """,
)

DROP_AUDIT_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events;",
    "DROP FUNCTION IF EXISTS audit_events_reject_mutation();",
)


def apply_rls() -> None:
    """Apply Row-Level Security to every tenant-owned table.

    Called from the Alembic migration so the policies are versioned and applied
    identically in every environment.
    """
    from alembic import op

    op.execute(CREATE_HELPER_FUNCTION)
    for table in RLS_TABLES:
        for statement in enable_policy_sql(table):
            op.execute(statement)
    for statement in AUDIT_IMMUTABILITY_STATEMENTS:
        op.execute(statement)


def remove_rls() -> None:
    """Reverse :func:`apply_rls`."""
    from alembic import op

    for statement in DROP_AUDIT_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for table in RLS_TABLES:
        for statement in disable_policy_sql(table):
            op.execute(statement)
    op.execute(DROP_HELPER_FUNCTION)
