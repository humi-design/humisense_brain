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
"""Layer 1 tables that hold tenant-owned data and carry a NOT NULL organization_id."""

LAYER2_RLS_TABLES: tuple[str, ...] = (
    "ingest_batches",
    "raw_records",
    "canonical_records",
    "record_quarantine",
    "outbox_events",
    "record_processing_events",
)
"""Layer 2 tables. Applied by the Layer 2 migration, not by the Layer 1 one, so the
Layer 1 revision stays reproducible against a database that predates Layer 2."""

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


# --- Layer 2 ---------------------------------------------------------------------

# ``raw_records`` is the preserved source payload. It must be immutable once written, so a
# trigger rejects UPDATE. DELETE is intentionally allowed: retention policy and the
# connection cascade both delete raw rows, and a delete is an auditable lifecycle action,
# not a silent rewrite of history.
RAW_RECORD_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION raw_records_reject_update() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'raw_records is append-only; UPDATE is not permitted'
            USING ERRCODE = '42501';
    END;
    $$;
    """,
    "DROP TRIGGER IF EXISTS raw_records_immutable ON raw_records;",
    """
    CREATE TRIGGER raw_records_immutable
        BEFORE UPDATE ON raw_records
        FOR EACH ROW EXECUTE FUNCTION raw_records_reject_update();
    """,
)

DROP_RAW_RECORD_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS raw_records_immutable ON raw_records;",
    "DROP FUNCTION IF EXISTS raw_records_reject_update();",
)

# ``record_processing_events`` is the append-only ledger of state transitions. Like the
# audit trail, it rejects both UPDATE and DELETE so history cannot be rewritten.
PROCESSING_LEDGER_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION record_processing_events_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'record_processing_events is append-only; % is not permitted', TG_OP
            USING ERRCODE = '42501';
    END;
    $$;
    """,
    "DROP TRIGGER IF EXISTS record_processing_events_immutable ON record_processing_events;",
    """
    CREATE TRIGGER record_processing_events_immutable
        BEFORE UPDATE OR DELETE ON record_processing_events
        FOR EACH ROW EXECUTE FUNCTION record_processing_events_reject_mutation();
    """,
)

DROP_PROCESSING_LEDGER_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS record_processing_events_immutable ON record_processing_events;",
    "DROP FUNCTION IF EXISTS record_processing_events_reject_mutation();",
)


def apply_layer2_rls() -> None:
    """Apply RLS and the immutability triggers to the Layer 2 tables.

    Called from the Layer 2 Alembic migration. The helper function ``app_current_org()``
    is created by the Layer 1 migration; it is recreated here defensively so this migration
    is correct even when run standalone.
    """
    from alembic import op

    op.execute(CREATE_HELPER_FUNCTION)
    for table in LAYER2_RLS_TABLES:
        for statement in enable_policy_sql(table):
            op.execute(statement)
    for statement in RAW_RECORD_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for statement in PROCESSING_LEDGER_IMMUTABILITY_STATEMENTS:
        op.execute(statement)


def remove_layer2_rls() -> None:
    """Reverse :func:`apply_layer2_rls`."""
    from alembic import op

    for statement in DROP_PROCESSING_LEDGER_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for statement in DROP_RAW_RECORD_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for table in LAYER2_RLS_TABLES:
        for statement in disable_policy_sql(table):
            op.execute(statement)


# --- Layer 3 ---------------------------------------------------------------------

LAYER3_RLS_TABLES: tuple[str, ...] = (
    "analytics_jobs",
    "analytics_execution_logs",
    "data_quality_results",
    "data_readiness_results",
    "feature_definitions",
    "feature_values",
    "baseline_definitions",
    "baseline_versions",
    "analytics_rules",
    "analytics_rule_versions",
    "anomaly_detections",
    "trend_results",
    "change_point_results",
    "forecast_results",
    "correlation_results",
    "model_registry",
    "model_evaluations",
    "detection_events",
    "detection_event_history",
)
"""Layer 3 tables. Applied by the Layer 3 migration so the Layer 2 revision stays
reproducible against a database that predates Layer 3."""

# The execution log is a ledger of how a run progressed; it must not be rewritten.
ANALYTICS_LOG_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION analytics_execution_logs_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'analytics_execution_logs is append-only; % is not permitted', TG_OP
            USING ERRCODE = '42501';
    END;
    $$;
    """,
    "DROP TRIGGER IF EXISTS analytics_execution_logs_immutable ON analytics_execution_logs;",
    """
    CREATE TRIGGER analytics_execution_logs_immutable
        BEFORE UPDATE OR DELETE ON analytics_execution_logs
        FOR EACH ROW EXECUTE FUNCTION analytics_execution_logs_reject_mutation();
    """,
)

DROP_ANALYTICS_LOG_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS analytics_execution_logs_immutable ON analytics_execution_logs;",
    "DROP FUNCTION IF EXISTS analytics_execution_logs_reject_mutation();",
)

# The detection lifecycle history is the audit trail of a detection; it is append-only, so
# a status change cannot be silently rewritten to hide that an event was suppressed.
DETECTION_HISTORY_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION detection_event_history_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'detection_event_history is append-only; % is not permitted', TG_OP
            USING ERRCODE = '42501';
    END;
    $$;
    """,
    "DROP TRIGGER IF EXISTS detection_event_history_immutable ON detection_event_history;",
    """
    CREATE TRIGGER detection_event_history_immutable
        BEFORE UPDATE OR DELETE ON detection_event_history
        FOR EACH ROW EXECUTE FUNCTION detection_event_history_reject_mutation();
    """,
)

DROP_DETECTION_HISTORY_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS detection_event_history_immutable ON detection_event_history;",
    "DROP FUNCTION IF EXISTS detection_event_history_reject_mutation();",
)

# Baseline and rule versions are immutable once written: a new version is a new row, so
# history cannot be edited after the fact.
BASELINE_VERSION_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION baseline_versions_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'baseline_versions is immutable; % is not permitted', TG_OP
            USING ERRCODE = '42501';
    END;
    $$;
    """,
    "DROP TRIGGER IF EXISTS baseline_versions_immutable ON baseline_versions;",
    """
    CREATE TRIGGER baseline_versions_immutable
        BEFORE UPDATE OR DELETE ON baseline_versions
        FOR EACH ROW EXECUTE FUNCTION baseline_versions_reject_mutation();
    """,
)

DROP_BASELINE_VERSION_IMMUTABILITY_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS baseline_versions_immutable ON baseline_versions;",
    "DROP FUNCTION IF EXISTS baseline_versions_reject_mutation();",
)


def apply_layer3_rls() -> None:
    """Apply RLS and the immutability triggers to the Layer 3 tables.

    Called from the Layer 3 Alembic migration. ``app_current_org()`` is created by the
    Layer 1 migration; it is recreated here defensively so this migration is correct even
    when run standalone.
    """
    from alembic import op

    op.execute(CREATE_HELPER_FUNCTION)
    for table in LAYER3_RLS_TABLES:
        for statement in enable_policy_sql(table):
            op.execute(statement)
    for statement in ANALYTICS_LOG_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for statement in DETECTION_HISTORY_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for statement in BASELINE_VERSION_IMMUTABILITY_STATEMENTS:
        op.execute(statement)


def remove_layer3_rls() -> None:
    """Reverse :func:`apply_layer3_rls`."""
    from alembic import op

    for statement in DROP_BASELINE_VERSION_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for statement in DROP_DETECTION_HISTORY_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for statement in DROP_ANALYTICS_LOG_IMMUTABILITY_STATEMENTS:
        op.execute(statement)
    for table in LAYER3_RLS_TABLES:
        for statement in disable_policy_sql(table):
            op.execute(statement)
