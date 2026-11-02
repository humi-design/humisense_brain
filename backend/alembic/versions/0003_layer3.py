"""Layer 3 schema: analytics, baselines, rules, detections, models.

Revision ID: 0003_layer3
Revises: 0002_layer2
Create Date: 2026-01-03

Adds the Layer 3 tables, then applies the same Row-Level Security treatment Layers 1 and 2
use:

* ``ENABLE`` **and** ``FORCE`` Row-Level Security on every tenant-owned table, with a policy
  requiring ``organization_id = app_current_org()``.
* An ``UPDATE``/``DELETE``-rejecting trigger on ``analytics_execution_logs`` (append-only
  run ledger), ``detection_event_history`` (append-only lifecycle audit), and
  ``baseline_versions`` (immutable baseline versions).

Indexes follow query patterns: list by tenant, filter by tenant+entity+metric, and order by
tenant+time. JSONB is used only where the shape is genuinely variable (rule condition,
detection evidence, quality issues, counters).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from humisense.db.rls import apply_layer3_rls, remove_layer3_rls

revision: str = "0003_layer3"
down_revision: Union[str, None] = "0002_layer2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB()


def _uuid() -> sa.Column[object]:
    return sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True)


def _org() -> sa.Column[object]:
    return sa.Column(
        "organization_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    job_status = postgresql.ENUM(
        "queued", "running", "succeeded", "partial", "failed", "skipped",
        name="analytics_job_status", create_type=False,
    )
    detection_status = postgresql.ENUM(
        "open", "acknowledged", "investigating", "resolved", "suppressed", "recovered",
        name="detection_status", create_type=False,
    )
    model_status = postgresql.ENUM(
        "registered", "evaluated", "active", "inactive", "rolled_back",
        name="model_status", create_type=False,
    )
    for enum in (job_status, detection_status, model_status):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "analytics_jobs",
        _uuid(),
        _org(),
        sa.Column("job_key", sa.String(200), nullable=False),
        sa.Column("job_type", sa.String(80), nullable=False),
        sa.Column("entity_id", sa.String(200)),
        sa.Column("metric", sa.String(120)),
        sa.Column("status", job_status, nullable=False, server_default="queued"),
        sa.Column("watermark", sa.DateTime(timezone=True)),
        sa.Column("detections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "job_key", name="uq_analytics_jobs_org_key"),
    )
    op.create_index("ix_analytics_jobs_organization_id", "analytics_jobs", ["organization_id"])
    op.create_index("ix_analytics_jobs_org_status", "analytics_jobs", ["organization_id", "status"])
    op.create_index("ix_analytics_jobs_org_created", "analytics_jobs", ["organization_id", "created_at"])

    op.create_table(
        "analytics_execution_logs",
        _uuid(),
        _org(),
        sa.Column("job_id", sa.Uuid(as_uuid=True), sa.ForeignKey("analytics_jobs.id", ondelete="CASCADE")),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("counters", JSONB, nullable=False, server_default="{}"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_analytics_execution_logs_organization_id", "analytics_execution_logs", ["organization_id"]
    )
    op.create_index(
        "ix_analytics_execution_logs_job", "analytics_execution_logs", ["job_id", "created_at"]
    )

    op.create_table(
        "data_quality_results",
        _uuid(),
        _org(),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("evaluated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("issues", JSONB, nullable=False, server_default="[]"),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_id", "metric", "window_end", name="uq_data_quality_results_window"
        ),
    )
    op.create_index("ix_data_quality_results_organization_id", "data_quality_results", ["organization_id"])
    op.create_index(
        "ix_data_quality_results_org_entity", "data_quality_results",
        ["organization_id", "entity_id", "metric"],
    )

    op.create_table(
        "data_readiness_results",
        _uuid(),
        _org(),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("method", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reasons", JSONB, nullable=False, server_default="[]"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("min_required", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_id", "metric", "method", "window_end",
            name="uq_data_readiness_results_window",
        ),
    )
    op.create_index("ix_data_readiness_results_organization_id", "data_readiness_results", ["organization_id"])
    op.create_index(
        "ix_data_readiness_results_org_entity", "data_readiness_results",
        ["organization_id", "entity_id", "metric"],
    )

    op.create_table(
        "feature_definitions",
        _uuid(),
        _org(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("method", sa.String(120), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(40)),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "name", "version", name="uq_feature_definitions_name_version"
        ),
    )
    op.create_index("ix_feature_definitions_organization_id", "feature_definitions", ["organization_id"])

    op.create_table(
        "feature_values",
        _uuid(),
        _org(),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(40)),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(120), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("sources", JSONB, nullable=False, server_default="[]"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_id", "metric", "name", "version", "at",
            name="uq_feature_values_observation",
        ),
    )
    op.create_index("ix_feature_values_organization_id", "feature_values", ["organization_id"])
    op.create_index(
        "ix_feature_values_org_entity", "feature_values", ["organization_id", "entity_id", "metric", "at"]
    )

    op.create_table(
        "baseline_definitions",
        _uuid(),
        _org(),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("baseline_type", sa.String(30), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(40)),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_id", "metric", "baseline_type",
            name="uq_baseline_definitions_identity",
        ),
    )
    op.create_index("ix_baseline_definitions_organization_id", "baseline_definitions", ["organization_id"])
    op.create_index(
        "ix_baseline_definitions_org_entity", "baseline_definitions",
        ["organization_id", "entity_id", "metric"],
    )

    op.create_table(
        "baseline_versions",
        _uuid(),
        _org(),
        sa.Column(
            "baseline_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("baseline_definitions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("mean", sa.Float()),
        sa.Column("median", sa.Float()),
        sa.Column("stddev", sa.Float()),
        sa.Column("minimum", sa.Float()),
        sa.Column("maximum", sa.Float()),
        sa.Column("p10", sa.Float()),
        sa.Column("p25", sa.Float()),
        sa.Column("p75", sa.Float()),
        sa.Column("p90", sa.Float()),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("dataset_ref", sa.String(200)),
        sa.Column("created_by", sa.Uuid(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "baseline_id", "version", name="uq_baseline_versions_version"),
    )
    op.create_index("ix_baseline_versions_organization_id", "baseline_versions", ["organization_id"])
    op.create_index("ix_baseline_versions_baseline", "baseline_versions", ["baseline_id", "version"])

    op.create_table(
        "analytics_rules",
        _uuid(),
        _org(),
        sa.Column("rule_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("domain", sa.String(30), nullable=False, server_default="generic"),
        sa.Column("scope", sa.String(40), nullable=False, server_default="entity"),
        sa.Column("entity_id", sa.String(200)),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("rule_id", name="uq_analytics_rules_rule_id"),
    )
    op.create_index("ix_analytics_rules_organization_id", "analytics_rules", ["organization_id"])

    op.create_table(
        "analytics_rule_versions",
        _uuid(),
        _org(),
        sa.Column(
            "rule_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("analytics_rules.rule_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("condition", JSONB, nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("severity", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("created_by", sa.Uuid(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "rule_id", "version", name="uq_analytics_rule_versions_version"
        ),
    )
    op.create_index(
        "ix_analytics_rule_versions_organization_id", "analytics_rule_versions", ["organization_id"]
    )
    op.create_index("ix_analytics_rule_versions_rule", "analytics_rule_versions", ["rule_id", "version"])

    op.create_table(
        "anomaly_detections",
        _uuid(),
        _org(),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("algorithm_name", sa.String(80), nullable=False),
        sa.Column("algorithm_version", sa.String(20), nullable=False),
        sa.Column("anomaly_score", sa.Float()),
        sa.Column("observed_value", sa.Float()),
        sa.Column("expected_value", sa.Float()),
        sa.Column("deviation", sa.Float()),
        sa.Column("baseline_id", sa.Uuid(as_uuid=True)),
        sa.Column("baseline_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(40)),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_id", "metric", "algorithm_name", "window_end",
            "baseline_version", name="uq_anomaly_detections_observation",
        ),
    )
    op.create_index("ix_anomaly_detections_organization_id", "anomaly_detections", ["organization_id"])
    op.create_index(
        "ix_anomaly_detections_org_entity", "anomaly_detections",
        ["organization_id", "entity_id", "metric"],
    )

    op.create_table(
        "trend_results",
        _uuid(),
        _org(),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("magnitude", sa.Float()),
        sa.Column("method", sa.String(80), nullable=False),
        sa.Column("r_squared", sa.Float()),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(40)),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_id", "metric", "window_end", name="uq_trend_results_window"
        ),
    )
    op.create_index("ix_trend_results_organization_id", "trend_results", ["organization_id"])
    op.create_index(
        "ix_trend_results_org_entity", "trend_results", ["organization_id", "entity_id", "metric"]
    )

    op.create_table(
        "change_point_results",
        _uuid(),
        _org(),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(80), nullable=False),
        sa.Column("previous_mean", sa.Float()),
        sa.Column("new_mean", sa.Float()),
        sa.Column("magnitude", sa.Float()),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(40)),
        sa.Column("uncertainty", sa.Text()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_id", "metric", "detected_at",
            name="uq_change_point_results_detected",
        ),
    )
    op.create_index("ix_change_point_results_organization_id", "change_point_results", ["organization_id"])
    op.create_index(
        "ix_change_point_results_org_entity", "change_point_results",
        ["organization_id", "entity_id", "metric"],
    )

    op.create_table(
        "forecast_results",
        _uuid(),
        _org(),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("model_name", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(20), nullable=False),
        sa.Column("horizon_steps", sa.Integer(), nullable=False),
        sa.Column("predicted", JSONB, nullable=False, server_default="[]"),
        sa.Column("lower_bound", JSONB, nullable=False, server_default="[]"),
        sa.Column("upper_bound", JSONB, nullable=False, server_default="[]"),
        sa.Column("evaluation", JSONB, nullable=False, server_default="{}"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(40)),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("limitations", JSONB, nullable=False, server_default="[]"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_id", "metric", "generated_at", "horizon_steps",
            name="uq_forecast_results_generation",
        ),
    )
    op.create_index("ix_forecast_results_organization_id", "forecast_results", ["organization_id"])
    op.create_index(
        "ix_forecast_results_org_entity", "forecast_results", ["organization_id", "entity_id", "metric"]
    )

    op.create_table(
        "correlation_results",
        _uuid(),
        _org(),
        sa.Column("entity_a", sa.String(200), nullable=False),
        sa.Column("metric_a", sa.String(120), nullable=False),
        sa.Column("entity_b", sa.String(200), nullable=False),
        sa.Column("metric_b", sa.String(120), nullable=False),
        sa.Column("method", sa.String(40), nullable=False),
        sa.Column("coefficient", sa.Float()),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lag_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("caveat", sa.Text(), nullable=False, server_default=""),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "entity_a", "metric_a", "entity_b", "metric_b", "method", "window_end",
            name="uq_correlation_results_observation",
        ),
    )
    op.create_index("ix_correlation_results_organization_id", "correlation_results", ["organization_id"])
    op.create_index(
        "ix_correlation_results_org_pair", "correlation_results",
        ["organization_id", "entity_a", "entity_b"],
    )

    op.create_table(
        "model_registry",
        _uuid(),
        _org(),
        sa.Column("model_key", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("model_type", sa.String(80), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("status", model_status, nullable=False, server_default="registered"),
        sa.Column("dataset_ref", sa.String(200)),
        sa.Column("feature_versions", JSONB, nullable=False, server_default="{}"),
        sa.Column("training_params", JSONB, nullable=False, server_default="{}"),
        sa.Column("evaluation", JSONB, nullable=False, server_default="{}"),
        sa.Column("artifact_ref", sa.String(200)),
        sa.Column("owner", sa.Uuid(as_uuid=True)),
        sa.Column("rollback_version", sa.String(40)),
        sa.Column("trained_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "model_key", "version", name="uq_model_registry_key_version"),
    )
    op.create_index("ix_model_registry_organization_id", "model_registry", ["organization_id"])
    op.create_index("ix_model_registry_org_status", "model_registry", ["organization_id", "status"])

    op.create_table(
        "model_evaluations",
        _uuid(),
        _org(),
        sa.Column(
            "model_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("model_registry.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("dataset_ref", sa.String(200), nullable=False),
        sa.Column("metrics", JSONB, nullable=False, server_default="{}"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text()),
        sa.Column("evaluated_by", sa.Uuid(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "model_id", "dataset_ref", name="uq_model_evaluations_dataset"
        ),
    )
    op.create_index("ix_model_evaluations_organization_id", "model_evaluations", ["organization_id"])
    op.create_index("ix_model_evaluations_model", "model_evaluations", ["model_id"])

    op.create_table(
        "detection_events",
        _uuid(),
        _org(),
        sa.Column("detection_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("domain", sa.String(30), nullable=False),
        sa.Column("source_system", sa.String(120), nullable=False, server_default=""),
        sa.Column("connection_id", sa.Uuid(as_uuid=True)),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("entity_type", sa.String(120)),
        sa.Column("metric", sa.String(120), nullable=False),
        sa.Column("detection_category", sa.String(40), nullable=False),
        sa.Column("detection_type", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("observed_value", sa.Float()),
        sa.Column("expected_value", sa.Float()),
        sa.Column("deviation", sa.Float()),
        sa.Column("unit", sa.String(40)),
        sa.Column("observation_window_start", sa.DateTime(timezone=True)),
        sa.Column("observation_window_end", sa.DateTime(timezone=True)),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("algorithm_name", sa.String(80), nullable=False),
        sa.Column("algorithm_version", sa.String(20), nullable=False),
        sa.Column("model_id", sa.Uuid(as_uuid=True)),
        sa.Column("model_version", sa.String(40)),
        sa.Column("baseline_id", sa.Uuid(as_uuid=True)),
        sa.Column("baseline_version", sa.Integer()),
        sa.Column("feature_versions", JSONB, nullable=False, server_default="{}"),
        sa.Column("anomaly_score", sa.Float()),
        sa.Column("confidence", sa.Float()),
        sa.Column("data_quality_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("evidence_references", JSONB, nullable=False, server_default="[]"),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("limitations", JSONB, nullable=False, server_default="[]"),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("status", detection_status, nullable=False, server_default="open"),
        sa.Column("group_key", sa.String(200)),
        sa.Column("suppressed_until", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.Uuid(as_uuid=True)),
        sa.Column("resolved_by", sa.Uuid(as_uuid=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "detection_id", name="uq_detection_events_detection_id"),
    )
    op.create_index("ix_detection_events_organization_id", "detection_events", ["organization_id"])
    op.create_index("ix_detection_events_org_status", "detection_events", ["organization_id", "status"])
    op.create_index(
        "ix_detection_events_org_entity", "detection_events", ["organization_id", "entity_id", "metric"]
    )
    op.create_index("ix_detection_events_org_detected", "detection_events", ["organization_id", "detected_at"])
    op.create_index("ix_detection_events_org_severity", "detection_events", ["organization_id", "severity"])

    op.create_table(
        "detection_event_history",
        _uuid(),
        _org(),
        sa.Column("detection_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(30)),
        sa.Column("to_status", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True)),
        sa.Column("reason", sa.Text()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_detection_event_history_organization_id", "detection_event_history", ["organization_id"]
    )
    op.create_index(
        "ix_detection_event_history_detection", "detection_event_history", ["detection_id", "created_at"]
    )

    apply_layer3_rls()


def downgrade() -> None:
    remove_layer3_rls()
    for table in (
        "detection_event_history",
        "detection_events",
        "model_evaluations",
        "model_registry",
        "correlation_results",
        "forecast_results",
        "change_point_results",
        "trend_results",
        "anomaly_detections",
        "analytics_rule_versions",
        "analytics_rules",
        "baseline_versions",
        "baseline_definitions",
        "feature_values",
        "feature_definitions",
        "data_readiness_results",
        "data_quality_results",
        "analytics_execution_logs",
        "analytics_jobs",
    ):
        op.drop_table(table)
    for name in ("model_status", "detection_status", "analytics_job_status"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
