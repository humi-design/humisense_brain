"""Layer 3 repository: the only place Layer 3 touches the database.

Every read and write runs inside a ``tenant_session``, so the Row-Level Security GUC is set
for the transaction and a query written without a tenant filter still cannot cross a tenant
boundary. The repository does not take a session from the caller: it opens its own, which
keeps tenant scoping impossible to forget.

Writes are idempotent on each table's natural key (``ON CONFLICT DO UPDATE``), so a re-run
over the same window upserts rather than duplicating. This is what makes scheduled and
backfill execution safe to repeat.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult

from humisense.db.models import (
    AnalyticsExecutionLog,
    AnalyticsJob,
    AnalyticsRule,
    AnalyticsRuleVersionRow,
    AnomalyDetectionRow,
    BaselineDefinition,
    BaselineVersionRow,
    CanonicalRecord,
    ChangePointResultRow,
    CorrelationResultRow,
    DataQualityResultRow,
    DataReadinessResultRow,
    DetectionEventHistoryRow,
    DetectionEventRow,
    FeatureValueRow,
    ForecastResultRow,
    JobStatus,
    ModelRegistryRow,
    Organization,
    TrendResultRow,
)
from humisense.db.session import Database


class AnalyticsRepository:
    """Tenant-scoped persistence and window reads for Layer 3."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- Reads ------------------------------------------------------------

    async def read_canonical_window(
        self,
        organization_id: uuid.UUID,
        *,
        connection_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10_000,
        data_families: tuple[str, ...] | None = None,
    ) -> list[CanonicalRecord]:
        """Read canonical records in a bounded window, oldest first.

        The window is bounded by ``limit`` so a run never loads an unbounded history. The
        query is ordered by event time (falling back to source time) so downstream windowing
        is deterministic.
        """
        from sqlalchemy import func

        async with self._database.tenant_session(organization_id) as session:
            statement = select(CanonicalRecord).where(
                CanonicalRecord.organization_id == organization_id
            )
            if connection_id is not None:
                statement = statement.where(CanonicalRecord.connection_id == connection_id)
            event_time = func.coalesce(
                CanonicalRecord.event_timestamp, CanonicalRecord.source_timestamp
            )
            if since is not None:
                statement = statement.where(event_time >= since)
            if until is not None:
                statement = statement.where(event_time <= until)
            if data_families is not None:
                statement = statement.where(CanonicalRecord.data_family.in_(data_families))
            statement = statement.order_by(event_time.asc().nulls_last()).limit(limit)
            return list((await session.scalars(statement)).all())

    async def list_metrics(self, organization_id: uuid.UUID, *, limit: int = 500) -> list[str]:
        """Return the distinct record types seen for a tenant, for discovery endpoints."""
        async with self._database.tenant_session(organization_id) as session:
            rows = (
                await session.scalars(
                    select(CanonicalRecord.record_type)
                    .where(CanonicalRecord.organization_id == organization_id)
                    .distinct()
                    .limit(limit)
                )
            ).all()
            return sorted({str(r) for r in rows})

    async def latest_watermark(
        self,
        organization_id: uuid.UUID,
        *,
        job_type: str,
        entity_id: str | None,
        metric: str | None,
    ) -> datetime | None:
        """Return the newest event time a previous run of this job processed."""
        job_key = self._job_key(job_type, entity_id, metric)
        async with self._database.tenant_session(organization_id) as session:
            job = await session.scalar(
                select(AnalyticsJob).where(
                    AnalyticsJob.organization_id == organization_id,
                    AnalyticsJob.job_key == job_key,
                )
            )
        return job.watermark if job is not None else None

    # --- Jobs and logs ----------------------------------------------------

    @staticmethod
    def _job_key(job_type: str, entity_id: str | None, metric: str | None) -> str:
        return f"{job_type}:{entity_id or '*'}:{metric or '*'}"

    async def upsert_job(
        self,
        organization_id: uuid.UUID,
        *,
        job_type: str,
        entity_id: str | None,
        metric: str | None,
        status: JobStatus,
        correlation_id: str,
        watermark: datetime | None = None,
        detections: int = 0,
        error_code: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> uuid.UUID:
        job_key = self._job_key(job_type, entity_id, metric)
        async with self._database.tenant_session(organization_id) as session:
            statement = (
                pg_insert(AnalyticsJob)
                .values(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    job_key=job_key,
                    job_type=job_type,
                    entity_id=entity_id,
                    metric=metric,
                    status=status,
                    watermark=watermark,
                    detections=detections,
                    error_code=error_code,
                    correlation_id=correlation_id,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                .on_conflict_do_update(
                    constraint="uq_analytics_jobs_org_key",
                    set_={
                        "status": status,
                        "watermark": watermark,
                        "detections": detections,
                        "error_code": error_code,
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "correlation_id": correlation_id,
                    },
                )
                .returning(AnalyticsJob.id)
            )
            job_id = await session.scalar(statement)
        return uuid.UUID(str(job_id))

    async def log_execution(
        self,
        organization_id: uuid.UUID,
        *,
        job_id: uuid.UUID | None,
        stage: str,
        status: str,
        correlation_id: str,
        detail: str | None = None,
        counters: dict[str, Any] | None = None,
    ) -> None:
        async with self._database.tenant_session(organization_id) as session:
            session.add(
                AnalyticsExecutionLog(
                    organization_id=organization_id,
                    job_id=job_id,
                    stage=stage,
                    status=status,
                    detail=detail,
                    counters=counters or {},
                    correlation_id=correlation_id,
                )
            )

    # --- Quality / readiness ---------------------------------------------

    async def list_organization_ids(self) -> list[uuid.UUID]:
        """Return every tenant id, for a background sweep.

        This is the only read that crosses tenants, and it is used only by the worker's
        scheduled loop, which then drains each tenant inside its own tenant-scoped session.
        """
        async with self._database.system_session() as session:
            return list(
                (await session.scalars(select(Organization.id).order_by(Organization.id))).all()
            )

    async def list_quality_results(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        entity_id: str | None = None,
    ) -> list[DataQualityResultRow]:
        async with self._database.tenant_session(organization_id) as session:
            statement = select(DataQualityResultRow).where(
                DataQualityResultRow.organization_id == organization_id
            )
            if status:
                statement = statement.where(DataQualityResultRow.status == status)
            if entity_id:
                statement = statement.where(DataQualityResultRow.entity_id == entity_id)
            statement = (
                statement.order_by(DataQualityResultRow.window_end.desc())
                .limit(limit)
                .offset(offset)
            )
            return list((await session.scalars(statement)).all())

    async def upsert_quality(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            statement = pg_insert(DataQualityResultRow).values(
                id=uuid.uuid4(), organization_id=organization_id, **values
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_data_quality_results_window",
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in {"entity_id", "metric", "window_end"}
                },
            )
            await session.execute(statement)

    async def upsert_readiness(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            statement = pg_insert(DataReadinessResultRow).values(
                id=uuid.uuid4(), organization_id=organization_id, **values
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_data_readiness_results_window",
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in {"entity_id", "metric", "method", "window_end"}
                },
            )
            await session.execute(statement)

    # --- Features ---------------------------------------------------------

    async def upsert_feature_values(
        self, organization_id: uuid.UUID, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        async with self._database.tenant_session(organization_id) as session:
            for values in rows:
                statement = pg_insert(FeatureValueRow).values(
                    id=uuid.uuid4(), organization_id=organization_id, **values
                )
                statement = statement.on_conflict_do_update(
                    constraint="uq_feature_values_observation",
                    set_={
                        k: v
                        for k, v in values.items()
                        if k not in {"entity_id", "metric", "name", "version", "at"}
                    },
                )
                await session.execute(statement)

    # --- Baselines --------------------------------------------------------

    async def get_or_create_baseline_definition(
        self,
        organization_id: uuid.UUID,
        *,
        entity_id: str,
        metric: str,
        baseline_type: str,
        window_seconds: int,
        unit: str | None,
    ) -> uuid.UUID:
        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(BaselineDefinition).where(
                    BaselineDefinition.organization_id == organization_id,
                    BaselineDefinition.entity_id == entity_id,
                    BaselineDefinition.metric == metric,
                    BaselineDefinition.baseline_type == baseline_type,
                )
            )
            if existing is not None:
                return existing.id
            definition = BaselineDefinition(
                organization_id=organization_id,
                entity_id=entity_id,
                metric=metric,
                baseline_type=baseline_type,
                window_seconds=window_seconds,
                unit=unit,
                current_version=0,
                enabled=True,
            )
            session.add(definition)
            await session.flush()
            return definition.id

    async def add_baseline_version(
        self,
        organization_id: uuid.UUID,
        *,
        baseline_id: uuid.UUID,
        version: int,
        values: dict[str, Any],
    ) -> None:
        async with self._database.tenant_session(organization_id) as session:
            session.add(
                BaselineVersionRow(
                    organization_id=organization_id,
                    baseline_id=baseline_id,
                    version=version,
                    **values,
                )
            )
            definition = await session.scalar(
                select(BaselineDefinition).where(
                    BaselineDefinition.organization_id == organization_id,
                    BaselineDefinition.id == baseline_id,
                )
            )
            if definition is not None:
                definition.current_version = version

    async def latest_baseline_version(
        self, organization_id: uuid.UUID, *, baseline_id: uuid.UUID
    ) -> BaselineVersionRow | None:
        async with self._database.tenant_session(organization_id) as session:
            row: BaselineVersionRow | None = await session.scalar(
                select(BaselineVersionRow)
                .where(
                    BaselineVersionRow.organization_id == organization_id,
                    BaselineVersionRow.baseline_id == baseline_id,
                )
                .order_by(BaselineVersionRow.version.desc())
                .limit(1)
            )
        return row

    # --- Rules ------------------------------------------------------------

    async def upsert_rule(
        self,
        organization_id: uuid.UUID,
        *,
        rule_id: uuid.UUID,
        name: str,
        description: str,
        domain: str,
        scope: str,
        entity_id: str | None,
        metric: str,
        condition: dict[str, Any],
        window_seconds: int,
        severity: str,
        enabled: bool,
    ) -> int:
        """Create or update a rule, adding a new immutable version when the condition changes."""
        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(AnalyticsRule).where(
                    AnalyticsRule.organization_id == organization_id,
                    AnalyticsRule.rule_id == rule_id,
                )
            )
            if existing is None:
                version = 1
                session.add(
                    AnalyticsRule(
                        organization_id=organization_id,
                        rule_id=rule_id,
                        name=name,
                        description=description,
                        domain=domain,
                        scope=scope,
                        entity_id=entity_id,
                        metric=metric,
                        current_version=version,
                        enabled=enabled,
                    )
                )
            else:
                existing.name = name
                existing.description = description
                existing.domain = domain
                existing.scope = scope
                existing.entity_id = entity_id
                existing.metric = metric
                existing.enabled = enabled
                latest = await session.scalar(
                    select(AnalyticsRuleVersionRow)
                    .where(
                        AnalyticsRuleVersionRow.organization_id == organization_id,
                        AnalyticsRuleVersionRow.rule_id == rule_id,
                    )
                    .order_by(AnalyticsRuleVersionRow.version.desc())
                    .limit(1)
                )
                if (
                    latest is not None
                    and latest.condition == condition
                    and latest.window_seconds == window_seconds
                    and latest.severity == severity
                ):
                    return latest.version
                version = (latest.version + 1) if latest is not None else 1
                existing.current_version = version
            session.add(
                AnalyticsRuleVersionRow(
                    organization_id=organization_id,
                    rule_id=rule_id,
                    version=version,
                    condition=condition,
                    window_seconds=window_seconds,
                    severity=severity,
                )
            )
            return version

    async def list_rules(
        self, organization_id: uuid.UUID, *, metric: str | None = None
    ) -> list[tuple[AnalyticsRule, AnalyticsRuleVersionRow]]:
        async with self._database.tenant_session(organization_id) as session:
            statement = select(AnalyticsRule).where(
                AnalyticsRule.organization_id == organization_id,
                AnalyticsRule.enabled.is_(True),
            )
            if metric is not None:
                statement = statement.where(AnalyticsRule.metric == metric)
            rules = list((await session.scalars(statement)).all())
            result: list[tuple[AnalyticsRule, AnalyticsRuleVersionRow]] = []
            for rule in rules:
                version_row = await session.scalar(
                    select(AnalyticsRuleVersionRow).where(
                        AnalyticsRuleVersionRow.organization_id == organization_id,
                        AnalyticsRuleVersionRow.rule_id == rule.rule_id,
                        AnalyticsRuleVersionRow.version == rule.current_version,
                    )
                )
                if version_row is not None:
                    result.append((rule, version_row))
            return result

    # --- Detector outputs -------------------------------------------------

    async def upsert_anomaly(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            statement = pg_insert(AnomalyDetectionRow).values(
                id=uuid.uuid4(), organization_id=organization_id, **values
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_anomaly_detections_observation",
                set_={
                    k: v
                    for k, v in values.items()
                    if k
                    not in {
                        "entity_id",
                        "metric",
                        "algorithm_name",
                        "window_end",
                        "baseline_version",
                    }
                },
            )
            await session.execute(statement)

    async def upsert_trend(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            statement = pg_insert(TrendResultRow).values(
                id=uuid.uuid4(), organization_id=organization_id, **values
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_trend_results_window",
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in {"entity_id", "metric", "window_end"}
                },
            )
            await session.execute(statement)

    async def upsert_change_point(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            statement = pg_insert(ChangePointResultRow).values(
                id=uuid.uuid4(), organization_id=organization_id, **values
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_change_point_results_detected",
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in {"entity_id", "metric", "detected_at"}
                },
            )
            await session.execute(statement)

    async def upsert_forecast(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            statement = pg_insert(ForecastResultRow).values(
                id=uuid.uuid4(), organization_id=organization_id, **values
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_forecast_results_generation",
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in {"entity_id", "metric", "generated_at", "horizon_steps"}
                },
            )
            await session.execute(statement)

    async def upsert_correlation(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            statement = pg_insert(CorrelationResultRow).values(
                id=uuid.uuid4(), organization_id=organization_id, **values
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_correlation_results_observation",
                set_={
                    k: v
                    for k, v in values.items()
                    if k
                    not in {
                        "entity_a",
                        "metric_a",
                        "entity_b",
                        "metric_b",
                        "method",
                        "window_end",
                    }
                },
            )
            await session.execute(statement)

    # --- Models -----------------------------------------------------------

    async def register_model(self, organization_id: uuid.UUID, values: dict[str, Any]) -> uuid.UUID:
        async with self._database.tenant_session(organization_id) as session:
            row = ModelRegistryRow(organization_id=organization_id, **values)
            session.add(row)
            await session.flush()
            return row.id

    async def list_models(self, organization_id: uuid.UUID) -> list[ModelRegistryRow]:
        async with self._database.tenant_session(organization_id) as session:
            return list(
                (
                    await session.scalars(
                        select(ModelRegistryRow)
                        .where(ModelRegistryRow.organization_id == organization_id)
                        .order_by(ModelRegistryRow.created_at.desc())
                    )
                ).all()
            )

    # --- Detections -------------------------------------------------------

    async def insert_detection(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> uuid.UUID:
        async with self._database.tenant_session(organization_id) as session:
            row = DetectionEventRow(organization_id=organization_id, **values)
            session.add(row)
            await session.flush()
            session.add(
                DetectionEventHistoryRow(
                    organization_id=organization_id,
                    detection_id=row.detection_id,
                    from_status=None,
                    to_status=row.status.value,
                    correlation_id=row.correlation_id,
                    reason="created",
                )
            )
            return row.id

    async def find_recent_detection(
        self,
        organization_id: uuid.UUID,
        *,
        entity_id: str,
        metric: str,
        algorithm_name: str,
        since: datetime,
    ) -> DetectionEventRow | None:
        """Return a matching detection created at or after ``since``, for cooldown grouping."""
        async with self._database.tenant_session(organization_id) as session:
            row: DetectionEventRow | None = await session.scalar(
                select(DetectionEventRow)
                .where(
                    DetectionEventRow.organization_id == organization_id,
                    DetectionEventRow.entity_id == entity_id,
                    DetectionEventRow.metric == metric,
                    DetectionEventRow.algorithm_name == algorithm_name,
                    DetectionEventRow.created_at >= since,
                )
                .order_by(DetectionEventRow.created_at.desc())
                .limit(1)
            )
        return row

    async def list_detections(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        severity: str | None = None,
        status: str | None = None,
        entity_id: str | None = None,
    ) -> list[DetectionEventRow]:
        async with self._database.tenant_session(organization_id) as session:
            statement = select(DetectionEventRow).where(
                DetectionEventRow.organization_id == organization_id
            )
            if severity:
                statement = statement.where(DetectionEventRow.severity == severity)
            if status:
                statement = statement.where(DetectionEventRow.status == status)
            if entity_id:
                statement = statement.where(DetectionEventRow.entity_id == entity_id)
            statement = (
                statement.order_by(DetectionEventRow.detected_at.desc()).limit(limit).offset(offset)
            )
            return list((await session.scalars(statement)).all())

    async def get_detection(
        self, organization_id: uuid.UUID, *, detection_id: uuid.UUID
    ) -> DetectionEventRow | None:
        async with self._database.tenant_session(organization_id) as session:
            row: DetectionEventRow | None = await session.scalar(
                select(DetectionEventRow).where(
                    DetectionEventRow.organization_id == organization_id,
                    DetectionEventRow.detection_id == detection_id,
                )
            )
        return row

    async def update_detection_status(
        self,
        organization_id: uuid.UUID,
        *,
        detection_id: uuid.UUID,
        to_status: str,
        actor_id: uuid.UUID | None,
        reason: str | None,
        correlation_id: str,
        resolved: bool = False,
        suppressed_until: datetime | None = None,
    ) -> DetectionEventRow | None:
        async with self._database.tenant_session(organization_id) as session:
            row = await session.scalar(
                select(DetectionEventRow).where(
                    DetectionEventRow.organization_id == organization_id,
                    DetectionEventRow.detection_id == detection_id,
                )
            )
            if row is None:
                return None
            from_status = row.status.value
            row.status = to_status  # type: ignore[assignment]
            if resolved:
                row.resolved_by = actor_id
                row.resolved_at = datetime.now(tz=row.detected_at.tzinfo)
            if suppressed_until is not None:
                row.suppressed_until = suppressed_until
            session.add(
                DetectionEventHistoryRow(
                    organization_id=organization_id,
                    detection_id=detection_id,
                    from_status=from_status,
                    to_status=to_status,
                    actor_id=actor_id,
                    reason=reason,
                    correlation_id=correlation_id,
                )
            )
            return row

    async def delete_detections_for_tenant(self, organization_id: uuid.UUID) -> int:
        """Test/maintenance helper: remove detections for one tenant."""
        async with self._database.tenant_session(organization_id) as session:
            result = await session.execute(
                delete(DetectionEventRow).where(
                    DetectionEventRow.organization_id == organization_id
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)


__all__ = ["AnalyticsRepository"]
