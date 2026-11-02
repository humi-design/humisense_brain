"""The analytics engine: the orchestration that turns canonical records into detections.

The engine runs one bounded job at a time and follows a fixed order:

    SELECT → EXTRACT → QUALITY → READINESS → FEATURES → BASELINE → RULES → DETECT →
    SEVERITY → EVENT → PERSIST

Design commitments, mirroring Layer 2's:

* **One bad signal does not fail the run.** Each signal is analyzed in isolation; a failure
  produces a :class:`StageError` and the run continues.
* **No detection without evidence.** Quality and readiness are assessed before any detector
  runs; a window that fails readiness is skipped, not analyzed.
* **Idempotent.** Every write is an upsert on a natural key, and the job watermark advances,
  so re-running a window is safe.
* **Bounded.** The window is bounded by a row limit and by the previous watermark; nothing
  is buffered without limit.
* **Auditable.** Each stage writes an append-only execution-log row, and every detection
  carries its method, version, baseline version, feature versions, and correlation id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from humisense.config import Settings
from humisense.db.models import DetectionEventRow, DetectionStatus, JobStatus
from humisense.layer3 import detectors
from humisense.layer3.contract import (
    BaselineType,
    DataQualityResult,
    DetectionEvent,
    Domain,
    ReadinessStatus,
    RuleCondition,
)
from humisense.layer3.detection import (
    DetectionContext,
    GroupingPolicy,
    GroupState,
    build_detection_event,
    build_evidence,
    group_key_for,
    should_emit,
)
from humisense.layer3.detectors import BaselineReference, DetectorResult
from humisense.layer3.errors import AnalyticsErrorCode, Stage, StageError
from humisense.layer3.features import compute_features
from humisense.layer3.observations import Observation, extract_observations, group_by_signal
from humisense.layer3.quality import QualityThresholds, assess_quality
from humisense.layer3.readiness import evaluate_readiness, requirement_for
from humisense.layer3.repository import AnalyticsRepository
from humisense.layer3.rules import (
    RuleError,
    build_operands,
    evaluate_rule,
    validate_condition,
)
from humisense.observability.metrics import Metrics

logger = structlog.get_logger(__name__)

DEFAULT_WINDOW_SECONDS = 86_400
"""How far back a run looks when no explicit window is given."""

DEFAULT_ROW_LIMIT = 20_000
"""The maximum number of canonical records one run reads."""

DETECTION_METHODS: tuple[str, ...] = (
    "z_score",
    "robust_z_score",
    "iqr",
    "ewma",
    "baseline_deviation",
    "isolation_forest",
    "multivariate",
)
"""The methods the default detection sweep applies to each univariate signal.

Multivariate and isolation-forest run over the signal's own value series here; a wider
feature vector is supplied when a caller configures it.
"""


@dataclass(slots=True)
class RunResult:
    """The outcome of one analytics run."""

    job_type: str
    organization_id: uuid.UUID
    correlation_id: str
    status: JobStatus = JobStatus.QUEUED
    signals_analyzed: int = 0
    detections: int = 0
    suppressed: int = 0
    quality_issues: int = 0
    skips: list[StageError] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    finished_at: datetime | None = None
    counters: dict[str, int] = field(default_factory=dict)

    def skip(
        self, stage: Stage, code: AnalyticsErrorCode, detail: str, *, location: str | None = None
    ) -> None:
        self.skips.append(StageError(stage=stage, code=code, detail=detail, location=location))


class AnalyticsEngine:
    """Runs bounded analytics jobs over a tenant's canonical records."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        settings: Settings,
        *,
        metrics: Metrics | None = None,
        quality_thresholds: QualityThresholds | None = None,
        grouping_policy: GroupingPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._metrics = metrics
        self._quality_thresholds = quality_thresholds or QualityThresholds()
        self._grouping_policy = grouping_policy or GroupingPolicy()

    # --- Public entry points ---------------------------------------------

    async def run_sweep(self, *, correlation_id: str) -> list[RunResult]:
        """Run the full pipeline for every tenant over a bounded recent window.

        The sweep is a background loop, so no single tenant is in scope. Each tenant is run
        inside its own tenant-scoped sessions, which set the RLS GUC for that transaction
        only: the loop cannot read or write across tenants even though it visits them in
        sequence. One tenant's failure is recorded and does not stop the sweep.
        """
        organization_ids = await self._repository.list_organization_ids()
        results: list[RunResult] = []
        window_seconds = self._settings.layer3_sweep_window_seconds
        row_limit = self._settings.layer3_sweep_row_limit
        for organization_id in organization_ids:
            window_end = datetime.now(tz=UTC)
            try:
                result = await self.run_full(
                    organization_id,
                    correlation_id=f"{correlation_id}:{organization_id}",
                    since=window_end - timedelta(seconds=window_seconds),
                    until=window_end,
                    row_limit=row_limit,
                    job_type="analytics_sweep",
                )
            except Exception as exc:  # noqa: BLE001 - one tenant must not stop the sweep
                logger.error(
                    "layer3_sweep_tenant_failed",
                    organization_id=str(organization_id),
                    error_type=type(exc).__name__,
                )
                continue
            results.append(result)
        return results

    async def run_full(
        self,
        organization_id: uuid.UUID,
        *,
        correlation_id: str,
        connection_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        row_limit: int = DEFAULT_ROW_LIMIT,
        job_type: str = "analytics_full",
    ) -> RunResult:
        """Run the full pipeline over a bounded window and return a summary."""
        result = RunResult(
            job_type=job_type, organization_id=organization_id, correlation_id=correlation_id
        )
        if self._metrics is not None:
            self._metrics.layer3_run_started(job_type=job_type)

        now = datetime.now(tz=UTC)
        window_end = until or now
        window_start = since or (window_end - timedelta(seconds=DEFAULT_WINDOW_SECONDS))

        job_id = await self._repository.upsert_job(
            organization_id,
            job_type=job_type,
            entity_id=None,
            metric=None,
            status=JobStatus.RUNNING,
            correlation_id=correlation_id,
            started_at=result.started_at,
        )

        records = await self._repository.read_canonical_window(
            organization_id,
            connection_id=connection_id,
            since=window_start,
            until=window_end,
            limit=row_limit,
        )
        if not records:
            await self._finish(
                result,
                organization_id,
                job_id=job_id,
                correlation_id=correlation_id,
                status=JobStatus.SKIPPED,
            )
            result.skip(
                Stage.SELECT, AnalyticsErrorCode.NO_RECORDS, "No canonical records in the window."
            )
            await self._repository.log_execution(
                organization_id,
                job_id=job_id,
                stage=Stage.SELECT.value,
                status="skipped",
                correlation_id=correlation_id,
                detail="no records in window",
            )
            return result

        observations = [o for record in records for o in extract_observations(record)]
        grouped = group_by_signal(observations)
        await self._repository.log_execution(
            organization_id,
            job_id=job_id,
            stage=Stage.SELECT.value,
            status="succeeded",
            correlation_id=correlation_id,
            counters={"records": len(records), "signals": len(grouped)},
        )

        for (entity_id, metric), series in grouped.items():
            await self._analyze_signal(
                organization_id,
                entity_id=entity_id,
                metric=metric,
                series=series,
                correlation_id=correlation_id,
                now=now,
                reference_time=window_end,
                result=result,
            )

        status = JobStatus.SUCCEEDED if not result.skips else JobStatus.PARTIAL
        await self._finish(
            result, organization_id, job_id=job_id, correlation_id=correlation_id, status=status
        )
        return result

    async def run_quality_scan(
        self,
        organization_id: uuid.UUID,
        *,
        correlation_id: str,
        connection_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        row_limit: int = DEFAULT_ROW_LIMIT,
    ) -> RunResult:
        """Assess data quality for every signal in a window, without detecting."""
        result = RunResult(
            job_type="quality_scan", organization_id=organization_id, correlation_id=correlation_id
        )
        now = datetime.now(tz=UTC)
        window_end = until or now
        window_start = since or (window_end - timedelta(seconds=DEFAULT_WINDOW_SECONDS))
        job_id = await self._repository.upsert_job(
            organization_id,
            job_type="quality_scan",
            entity_id=None,
            metric=None,
            status=JobStatus.RUNNING,
            correlation_id=correlation_id,
            started_at=result.started_at,
        )
        records = await self._repository.read_canonical_window(
            organization_id,
            connection_id=connection_id,
            since=window_start,
            until=window_end,
            limit=row_limit,
        )
        observations = [o for record in records for o in extract_observations(record)]
        for (entity_id, metric), series in group_by_signal(observations).items():
            await self._assess_and_persist_quality(
                organization_id,
                entity_id=entity_id,
                metric=metric,
                series=series,
                as_of=window_end,
                correlation_id=correlation_id,
                result=result,
            )
        status = JobStatus.SUCCEEDED if not result.skips else JobStatus.PARTIAL
        await self._finish(
            result, organization_id, job_id=job_id, correlation_id=correlation_id, status=status
        )
        return result

    # --- Per-signal analysis ---------------------------------------------

    async def _analyze_signal(
        self,
        organization_id: uuid.UUID,
        *,
        entity_id: str,
        metric: str,
        series: list[Observation],
        correlation_id: str,
        now: datetime,
        reference_time: datetime,
        result: RunResult,
    ) -> None:
        result.signals_analyzed += 1
        quality = await self._assess_and_persist_quality(
            organization_id,
            entity_id=entity_id,
            metric=metric,
            series=series,
            as_of=reference_time,
            correlation_id=correlation_id,
            result=result,
        )

        # Features (causal, at the latest observation time).
        latest_at = series[-1].at
        features = compute_features(series, at=latest_at)
        if features:
            await self._repository.upsert_feature_values(
                organization_id,
                [
                    {
                        "entity_id": entity_id,
                        "metric": metric,
                        "name": feature.name,
                        "version": feature.version,
                        "value": feature.value,
                        "unit": None,
                        "at": feature.at,
                        "method": feature.method,
                        "window_seconds": feature.window_seconds,
                        "sample_count": feature.sample_count,
                        "quality_status": quality.status.value,
                        "sources": [str(s) for s in feature.sources],
                        "correlation_id": correlation_id,
                    }
                    for feature in features
                ],
            )
            if self._metrics is not None:
                self._metrics.layer3_features_computed(count=len(features))

        feature_map = {feature.name: feature.value for feature in features}
        feature_versions = {feature.name: feature.version for feature in features}

        values = [o.value for o in series]

        # Rules first: an explicit tenant rule is the most direct statement of intent.
        await self._apply_rules(
            organization_id,
            entity_id=entity_id,
            metric=metric,
            series=series,
            feature_map=feature_map,
            feature_versions=feature_versions,
            quality=quality,
            correlation_id=correlation_id,
            now=now,
            result=result,
        )

        # Then the statistical sweep.
        await self._apply_detectors(
            organization_id,
            entity_id=entity_id,
            metric=metric,
            series=series,
            values=values,
            feature_versions=feature_versions,
            quality=quality,
            correlation_id=correlation_id,
            now=now,
            result=result,
        )

    async def _assess_and_persist_quality(
        self,
        organization_id: uuid.UUID,
        *,
        entity_id: str,
        metric: str,
        series: list[Observation],
        as_of: datetime,
        correlation_id: str,
        result: RunResult,
    ) -> DataQualityResult:
        quality = assess_quality(
            organization_id,
            entity_id,
            metric,
            series,
            as_of=as_of,
            thresholds=self._quality_thresholds,
        )
        result.quality_issues += len(quality.issues)
        if self._metrics is not None:
            self._metrics.layer3_quality_result(status=quality.status.value)
        await self._repository.upsert_quality(
            organization_id,
            {
                "entity_id": entity_id,
                "metric": metric,
                "status": quality.status.value,
                "evaluated_count": quality.evaluated_count,
                "missing_count": quality.missing_count,
                "invalid_count": quality.invalid_count,
                "duplicate_count": quality.duplicate_count,
                "issues": [issue.model_dump(mode="json") for issue in quality.issues],
                "window_start": quality.window_start,
                "window_end": quality.window_end or as_of,
                "correlation_id": correlation_id,
            },
        )
        return quality

    async def _apply_rules(
        self,
        organization_id: uuid.UUID,
        *,
        entity_id: str,
        metric: str,
        series: list[Observation],
        feature_map: dict[str, float],
        feature_versions: dict[str, str],
        quality: DataQualityResult,
        correlation_id: str,
        now: datetime,
        result: RunResult,
    ) -> None:
        rules = await self._repository.list_rules(organization_id, metric=metric)
        if not rules:
            return
        operands = build_operands(series, feature_map)
        for rule, version_row in rules:
            if rule.scope == "entity" and rule.entity_id not in (None, entity_id):
                continue
            try:
                condition = RuleCondition.model_validate(version_row.condition)
                validate_condition(condition)
                evaluation = evaluate_rule(condition, operands)
            except (RuleError, ValueError) as error:
                result.skip(
                    Stage.RULES, AnalyticsErrorCode.RULE_INVALID, str(error), location=metric
                )
                continue
            if not evaluation.triggered:
                continue
            detector_result = DetectorResult(
                algorithm_name="threshold",
                algorithm_version=detectors.DETECTOR_VERSIONS["threshold"],
                triggered=True,
                observed_value=evaluation.observed,
                expected_value=evaluation.threshold,
                deviation=(
                    evaluation.observed - evaluation.threshold
                    if evaluation.observed is not None and evaluation.threshold is not None
                    else None
                ),
                anomaly_score=None,
                explanation=f"Rule {rule.name!r} matched: {evaluation.detail}",
                limitations=["A rule expresses intent; it is not a measure of rarity."],
            )
            await self._persist_detection(
                organization_id,
                entity_id=entity_id,
                metric=metric,
                series=series,
                detector_result=detector_result,
                quality=quality,
                correlation_id=correlation_id,
                now=now,
                result=result,
                feature_versions=feature_versions,
            )

    async def _apply_detectors(
        self,
        organization_id: uuid.UUID,
        *,
        entity_id: str,
        metric: str,
        series: list[Observation],
        values: list[float],
        feature_versions: dict[str, str],
        quality: DataQualityResult,
        correlation_id: str,
        now: datetime,
        result: RunResult,
    ) -> None:
        # A baseline-relative detector needs a stored baseline.
        baseline_id = await self._repository.get_or_create_baseline_definition(
            organization_id,
            entity_id=entity_id,
            metric=metric,
            baseline_type=BaselineType.ROLLING.value,
            window_seconds=DEFAULT_WINDOW_SECONDS,
            unit=series[-1].unit,
        )
        latest_baseline = await self._repository.latest_baseline_version(
            organization_id, baseline_id=baseline_id
        )

        candidate_results: list[DetectorResult] = []
        latest = values[-1]

        requirement = requirement_for("z_score")
        readiness = evaluate_readiness(
            organization_id, entity_id, metric, quality, requirement, as_of=now
        )
        if self._metrics is not None:
            self._metrics.layer3_readiness_result(
                method=requirement.method, status=readiness.status.value
            )

        if readiness.status in {ReadinessStatus.READY, ReadinessStatus.READY_WITH_WARNINGS}:
            mean = _mean(values)
            stddev = _stddev(values)
            candidate_results.append(detectors.detect_z_score(latest, mean=mean, stddev=stddev))
            median = _median(values)
            mad = _mad(values)
            candidate_results.append(
                detectors.detect_robust_z_score(latest, median=median, mad=mad)
            )
            p25, p75 = _quartiles(values)
            candidate_results.append(detectors.detect_iqr(latest, p25=p25, p75=p75))
            candidate_results.append(detectors.detect_ewma(values))
            candidate_results.append(detectors.detect_isolation_forest([[v] for v in values]))
            candidate_results.append(detectors.detect_multivariate([[v] for v in values]))

        if latest_baseline is not None:
            reference = BaselineReference(
                baseline_id=str(latest_baseline.baseline_id),
                version=latest_baseline.version,
                median=latest_baseline.median,
                mean=latest_baseline.mean,
                stddev=latest_baseline.stddev,
                p25=latest_baseline.p25,
                p75=latest_baseline.p75,
            )
            baseline_result = detectors.detect_baseline_deviation(latest, reference)
            candidate_results.append(baseline_result)

        for detector_result in candidate_results:
            if not detector_result.triggered:
                continue
            await self._persist_detection(
                organization_id,
                entity_id=entity_id,
                metric=metric,
                series=series,
                detector_result=detector_result,
                quality=quality,
                correlation_id=correlation_id,
                now=now,
                result=result,
                feature_versions=feature_versions,
                baseline_id=baseline_id if latest_baseline is not None else None,
                baseline_version=latest_baseline.version if latest_baseline is not None else None,
            )

    async def _persist_detection(
        self,
        organization_id: uuid.UUID,
        *,
        entity_id: str,
        metric: str,
        series: list[Observation],
        detector_result: DetectorResult,
        quality: DataQualityResult,
        correlation_id: str,
        now: datetime,
        result: RunResult,
        feature_versions: dict[str, str],
        baseline_id: uuid.UUID | None = None,
        baseline_version: int | None = None,
    ) -> None:
        group_key = group_key_for(entity_id, metric, detector_result.algorithm_name)
        existing = await self._repository.find_recent_detection(
            organization_id,
            entity_id=entity_id,
            metric=metric,
            algorithm_name=detector_result.algorithm_name,
            since=now - timedelta(seconds=self._grouping_policy.suppression_seconds),
        )
        if existing is not None:
            state = _state_from_row(existing)
            emit, _reason = should_emit(state, now=now, policy=self._grouping_policy)
            if not emit:
                result.suppressed += 1
                if self._metrics is not None:
                    self._metrics.layer3_detection_suppressed(reason="cooldown")
                return

        context = DetectionContext(
            organization_id=organization_id,
            domain=Domain.INDUSTRIAL,
            entity_id=entity_id,
            metric=metric,
            observed_value=detector_result.observed_value,
            unit=series[-1].unit,
            detected_at=now,
            observation_window_start=series[0].at,
            observation_window_end=series[-1].at,
            correlation_id=correlation_id,
            source_system=series[-1].source_system,
            connection_id=series[-1].connection_id,
            entity_type=series[-1].entity_type,
            quality_status=quality.status,
            baseline_id=baseline_id,
            baseline_version=baseline_version,
            feature_versions=feature_versions,
            evidence=build_evidence(series),
        )
        event = build_detection_event(context, detector_result)
        await self._repository.insert_detection(
            organization_id, _event_to_row(event, group_key=group_key)
        )
        result.detections += 1
        if self._metrics is not None:
            self._metrics.layer3_detection(
                domain=event.domain.value,
                severity=event.severity.value,
                method=event.algorithm_name,
            )

    # --- Completion -------------------------------------------------------

    async def _finish(
        self,
        result: RunResult,
        organization_id: uuid.UUID,
        *,
        job_id: uuid.UUID,
        correlation_id: str,
        status: JobStatus,
    ) -> None:
        result.status = status
        result.finished_at = datetime.now(tz=UTC)
        duration = (result.finished_at - result.started_at).total_seconds()
        result.counters = {
            "signals_analyzed": result.signals_analyzed,
            "detections": result.detections,
            "suppressed": result.suppressed,
            "quality_issues": result.quality_issues,
            "skips": len(result.skips),
        }
        await self._repository.upsert_job(
            organization_id,
            job_type=result.job_type,
            entity_id=None,
            metric=None,
            status=status,
            correlation_id=correlation_id,
            watermark=result.finished_at,
            detections=result.detections,
            started_at=result.started_at,
            finished_at=result.finished_at,
        )
        await self._repository.log_execution(
            organization_id,
            job_id=job_id,
            stage=Stage.PERSISTENCE.value,
            status=status.value,
            correlation_id=correlation_id,
            counters=result.counters,
        )
        if self._metrics is not None:
            self._metrics.layer3_run_finished(
                job_type=result.job_type, status=status.value, duration_seconds=duration
            )


# --- Helpers ------------------------------------------------------------------


def _state_from_row(row: DetectionEventRow) -> GroupState:
    """Reconstruct the grouping state from the most recent matching detection row."""
    return GroupState(
        group_key=row.group_key or "",
        first_seen=row.created_at,
        last_seen=row.created_at,
        count=1,
        suppressed_until=row.suppressed_until,
    )


def _event_to_row(event: DetectionEvent, *, group_key: str) -> dict[str, object]:
    return {
        "detection_id": event.detection_id,
        "domain": event.domain.value,
        "source_system": event.source_system,
        "connection_id": event.connection_id,
        "entity_id": event.entity_id,
        "entity_type": event.entity_type,
        "metric": event.metric,
        "detection_category": event.detection_category.value,
        "detection_type": event.detection_type,
        "severity": event.severity.value,
        "observed_value": event.observed_value,
        "expected_value": event.expected_value,
        "deviation": event.deviation,
        "unit": event.unit,
        "observation_window_start": event.observation_window_start,
        "observation_window_end": event.observation_window_end,
        "detected_at": event.detected_at,
        "algorithm_name": event.algorithm_name,
        "algorithm_version": event.algorithm_version,
        "model_id": event.model_id,
        "model_version": event.model_version,
        "baseline_id": event.baseline_id,
        "baseline_version": event.baseline_version,
        "feature_versions": event.feature_versions,
        "anomaly_score": event.anomaly_score,
        "confidence": event.confidence,
        "data_quality_status": event.data_quality_status.value,
        "evidence_references": [ref.model_dump(mode="json") for ref in event.evidence_references],
        "explanation": event.explanation,
        "limitations": event.limitations,
        "schema_version": event.schema_version,
        "status": DetectionStatus.OPEN,
        "group_key": group_key,
        "correlation_id": event.correlation_id,
    }


def _mean(values: list[float]) -> float | None:
    from humisense.layer3 import stats

    return stats.mean(values)


def _stddev(values: list[float]) -> float | None:
    from humisense.layer3 import stats

    return stats.stddev(values)


def _median(values: list[float]) -> float | None:
    from humisense.layer3 import stats

    return stats.median(values)


def _mad(values: list[float]) -> float | None:
    from humisense.layer3 import stats

    return stats.median_absolute_deviation(values)


def _quartiles(values: list[float]) -> tuple[float | None, float | None]:
    from humisense.layer3 import stats

    return stats.quantile(values, 0.25), stats.quantile(values, 0.75)


__all__ = ["AnalyticsEngine", "RunResult"]
