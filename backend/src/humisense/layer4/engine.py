"""Layer 4 work intelligence engine.

Orchestrates a bounded derivation run over a tenant's canonical records and detections. The
engine is deliberately thin: the interesting rules live in the focused modules
(``derivation``, ``status``, ``deadlines``, ``dependencies``, ``blockers``, ``followups``),
and this module sequences them and persists the result.

Design commitments:

* **Per-record isolation.** One un-derivable record becomes a skip in :class:`WorkRunResult`;
  it never fails the run.
* **Idempotent.** Every write is an upsert on a natural key, so a re-run or a backfill is
  safe and does not double-count.
* **Bounded.** A run reads at most ``policy.max_derivations_per_run`` records and a bounded
  detection window; nothing is loaded without a limit.
* **Provenance-preserving.** Every derived row records the source reference that produced it.
* **No root-cause claims.** A detection is associated with work only when a verified
  reference exists (the work's ``machine_ref`` matches the detection's ``entity_id``). The
  engine never declares a cause.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from humisense.config import Settings
from humisense.db.models import (
    CanonicalRecord,
    FollowUpTargetType,
    WorkStatus,
)
from humisense.layer4 import blockers as blockers_module
from humisense.layer4 import events as events_module
from humisense.layer4.contract import WORK_SCHEMA_VERSION
from humisense.layer4.departments import DepartmentMapping, build_department_mapping
from humisense.layer4.derivation import (
    SourceRecordView,
    WorkDerivation,
    derive_work,
)
from humisense.layer4.followups import FollowUpInputs, evaluate_followups
from humisense.layer4.policy import WorkPolicy
from humisense.layer4.priorities import (
    PriorityInputs,
    compute_source_completeness,
    derive_rule_priority,
)
from humisense.layer4.repository import WorkRepository
from humisense.layer4.status import SourceStatusMapping
from humisense.layer4.views import is_due_soon, is_overdue
from humisense.observability.metrics import Metrics, WorkMetrics

logger = structlog.get_logger(__name__)

DEFAULT_WINDOW_HOURS = 24
DEFAULT_ROW_LIMIT = 5000


@dataclass(slots=True)
class WorkRunSkip:
    """A record that could not be derived, with the reason."""

    stage: str
    code: str
    detail: str
    source_record_key: str | None = None

    def as_metadata(self) -> dict[str, str]:
        return {"stage": self.stage, "code": self.code, "detail": self.detail}


@dataclass(slots=True)
class WorkRunResult:
    """The outcome of one derivation run."""

    organization_id: uuid.UUID
    correlation_id: str
    job_type: str
    status: str = "succeeded"
    records_scanned: int = 0
    work_created: int = 0
    work_updated: int = 0
    commitments_created: int = 0
    dependencies_identified: int = 0
    blockers_identified: int = 0
    followups_identified: int = 0
    conflicts_detected: int = 0
    events_enqueued: int = 0
    detections_associated: int = 0
    skips: list[WorkRunSkip] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    finished_at: datetime | None = None

    @property
    def counters(self) -> dict[str, int]:
        return {
            "records_scanned": self.records_scanned,
            "work_created": self.work_created,
            "work_updated": self.work_updated,
            "commitments_created": self.commitments_created,
            "dependencies_identified": self.dependencies_identified,
            "blockers_identified": self.blockers_identified,
            "followups_identified": self.followups_identified,
            "conflicts_detected": self.conflicts_detected,
            "events_enqueued": self.events_enqueued,
            "detections_associated": self.detections_associated,
        }

    def skip(self, *, stage: str, code: str, detail: str, source_record_key: str | None) -> None:
        self.skips.append(
            WorkRunSkip(
                stage=stage, code=code, detail=detail[:200], source_record_key=source_record_key
            )
        )


class WorkIntelligenceEngine:
    """Derives and maintains work intelligence for one tenant at a time."""

    def __init__(
        self,
        repository: WorkRepository,
        settings: Settings,
        *,
        metrics: Metrics | None = None,
        work_metrics: WorkMetrics | None = None,
        policy: WorkPolicy | None = None,
        status_mapping: SourceStatusMapping | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._metrics = metrics
        self._work_metrics = work_metrics
        self._policy = policy or WorkPolicy()
        self._status_mapping = status_mapping or SourceStatusMapping()

    @property
    def policy(self) -> WorkPolicy:
        return self._policy

    # --- Public entry points ---------------------------------------------

    async def run_sweep(self, *, correlation_id: str) -> list[WorkRunResult]:
        """Run derivation for every tenant over a bounded recent window.

        The sweep is a background loop, so no single tenant is in scope. Each tenant is run
        inside its own tenant-scoped sessions, so the loop cannot cross a tenant boundary
        even though it visits them in sequence. One tenant's failure does not stop the sweep.
        """
        organization_ids = await self._repository.list_organization_ids()
        results: list[WorkRunResult] = []
        window_hours = getattr(self._settings, "layer4_sweep_window_hours", DEFAULT_WINDOW_HOURS)
        row_limit = getattr(
            self._settings, "layer4_sweep_row_limit", self._policy.max_derivations_per_run
        )
        for organization_id in organization_ids:
            window_end = datetime.now(tz=UTC)
            try:
                result = await self.run(
                    organization_id,
                    correlation_id=f"{correlation_id}:{organization_id}",
                    since=window_end - timedelta(hours=window_hours),
                    until=window_end,
                    row_limit=row_limit,
                    job_type="work_sweep",
                )
            except Exception as exc:  # noqa: BLE001 - one tenant must not stop the sweep
                logger.error(
                    "layer4_sweep_tenant_failed",
                    organization_id=str(organization_id),
                    error_type=type(exc).__name__,
                )
                continue
            results.append(result)
        return results

    async def run(
        self,
        organization_id: uuid.UUID,
        *,
        correlation_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        row_limit: int | None = None,
        job_type: str = "work_full",
    ) -> WorkRunResult:
        """Derive work from canonical records in a bounded window."""
        result = WorkRunResult(
            organization_id=organization_id, correlation_id=correlation_id, job_type=job_type
        )
        limit = min(
            row_limit or self._policy.max_derivations_per_run, self._policy.max_derivations_per_run
        )
        records = await self._repository.read_canonical_window(
            organization_id, since=since, until=until, limit=limit
        )
        department_mapping = await self._department_mapping(organization_id)
        as_of = until or datetime.now(tz=UTC)

        for record in records:
            result.records_scanned += 1
            try:
                await self._derive_record(
                    record,
                    result=result,
                    department_mapping=department_mapping,
                    as_of=as_of,
                )
            except Exception as exc:  # noqa: BLE001 - per-record isolation
                result.skip(
                    stage="derivation",
                    code="derivation_failed",
                    detail=f"{type(exc).__name__}",
                    source_record_key=record.source_record_key,
                )
                if self._work_metrics:
                    self._work_metrics.layer4_skip(stage="derivation", reason="derivation_failed")

        await self._associate_detections(organization_id, result=result, as_of=as_of)
        result.finished_at = datetime.now(tz=UTC)
        result.status = "partial" if result.skips else "succeeded"
        if self._work_metrics:
            self._work_metrics.layer4_run_finished(
                job_type=job_type,
                status=result.status,
                duration_seconds=(result.finished_at - result.started_at).total_seconds(),
            )
        logger.info(
            "layer4_run_finished",
            organization_id=str(organization_id),
            job_type=job_type,
            status=result.status,
            **result.counters,
        )
        return result

    # --- Internals --------------------------------------------------------

    async def _department_mapping(self, organization_id: uuid.UUID) -> DepartmentMapping:
        departments = await self._repository.list_departments(organization_id)
        return build_department_mapping(
            [(row.department_key, row.id, row.domain) for row in departments]
        )

    async def _derive_record(
        self,
        record: CanonicalRecord,
        *,
        result: WorkRunResult,
        department_mapping: DepartmentMapping,
        as_of: datetime,
    ) -> None:
        view = _to_view(record)
        derivation = await derive_work(
            view,
            policy=self._policy,
            status_mapping=self._status_mapping,
            department_mapping=department_mapping,
        )
        if derivation is None:
            return

        work_id, created = await self._repository.upsert_work_entity(
            result.organization_id, _entity_values(derivation, view)
        )
        if created:
            result.work_created += 1
            await self._repository.append_timeline_event(
                result.organization_id,
                _timeline_values(
                    work_id=work_id,
                    event_type="work.created",
                    view=view,
                    correlation_id=result.correlation_id,
                    event_at=view.effective_event_time() or view.received_timestamp,
                    provenance=derivation.origin.value,
                ),
            )
            await self._enqueue(
                result,
                work_id=work_id,
                event_type=events_module.WorkEventType.WORK_ENTITY_CREATED,
                payload={"work_key": derivation.work_key, "work_type": derivation.work_type.value},
                suffix="created",
            )
        else:
            result.work_updated += 1
            await self._enqueue(
                result,
                work_id=work_id,
                event_type=events_module.WorkEventType.WORK_ENTITY_UPDATED,
                payload={"work_key": derivation.work_key},
                suffix="updated",
            )

        await self._repository.upsert_source_reference(
            result.organization_id,
            {
                "work_id": work_id,
                "canonical_record_id": view.canonical_record_id,
                "raw_record_id": view.raw_record_id,
                "source_system": view.source_system,
                "source_record_key": view.source_record_key,
                "source_record_type": view.record_type,
                "relationship": derivation.source_relationship.value,
                "confidence": derivation.confidence,
                "correlation_id": result.correlation_id,
            },
        )

        for assignment in derivation.assignments:
            await self._repository.upsert_assignment(
                result.organization_id,
                {
                    "work_id": work_id,
                    "participant_type": assignment.participant_type,
                    "user_id": assignment.user_id,
                    "department_id": assignment.department_id,
                    "external_name": assignment.external_name,
                    "role": assignment.role,
                    "verification": assignment.verification,
                    "confidence": assignment.confidence,
                    "correlation_id": result.correlation_id,
                },
            )
            if assignment.role.value == "owner" and assignment.user_id is not None:
                await self._enqueue(
                    result,
                    work_id=work_id,
                    event_type=events_module.WorkEventType.WORK_ASSIGNED,
                    payload={"user_id": str(assignment.user_id)},
                    suffix=f"owner:{assignment.user_id}",
                )

        for draft in derivation.commitments:
            commitment_id, previous = await self._repository.upsert_commitment(
                result.organization_id,
                {
                    "commitment_key": draft.commitment_key,
                    "work_id": work_id,
                    "commitment_type": draft.commitment_type,
                    "description": draft.description,
                    "committing_party": draft.committing_party,
                    "receiving_party": draft.receiving_party,
                    "responsible_owner": derivation.owner_id,
                    "related_customer_ref": draft.related_customer_ref,
                    "related_supplier_ref": draft.related_supplier_ref,
                    "related_order_ref": draft.related_order_ref,
                    "due_at": draft.due_at,
                    "commitment_status": draft.commitment_status,
                    "verification_status": draft.verification_status,
                    "confidence": draft.confidence,
                    "source_reference": draft.source_reference,
                    "correlation_id": result.correlation_id,
                },
            )
            if previous is None:
                result.commitments_created += 1
                await self._repository.append_timeline_event(
                    result.organization_id,
                    _timeline_values(
                        work_id=work_id,
                        event_type="commitment.created",
                        view=view,
                        correlation_id=result.correlation_id,
                        event_at=view.effective_event_time() or view.received_timestamp,
                        provenance=derivation.origin.value,
                    ),
                )
                await self._enqueue(
                    result,
                    work_id=work_id,
                    event_type=events_module.WorkEventType.COMMITMENT_IDENTIFIED,
                    payload={"commitment_id": str(commitment_id)},
                    suffix=f"commitment:{commitment_id}",
                )
            else:
                await self._repository.append_commitment_history(
                    result.organization_id,
                    {
                        "commitment_id": commitment_id,
                        "from_status": previous["commitment_status"],
                        "to_status": draft.commitment_status.value,
                        "previous_due_at": previous["due_at"],
                        "new_due_at": draft.due_at,
                        "reason": "source record revised the commitment",
                        "correlation_id": result.correlation_id,
                    },
                )
                await self._enqueue(
                    result,
                    work_id=work_id,
                    event_type=events_module.WorkEventType.COMMITMENT_UPDATED,
                    payload={"commitment_id": str(commitment_id)},
                    suffix=f"commitment-updated:{commitment_id}",
                )

        for dep in derivation.dependencies:
            dependency_id = await self._repository.upsert_dependency(
                result.organization_id,
                {
                    "source_work_id": work_id,
                    "target_work_id": None,
                    "target_entity_type": dep.target_entity_type,
                    "target_entity_ref": dep.target_entity_ref,
                    "relationship": dep.relationship,
                    "status": "unknown",
                    "origin": dep.origin,
                    "confidence": derivation.confidence,
                    "verification_status": dep.verification_status,
                    "correlation_id": result.correlation_id,
                },
            )
            result.dependencies_identified += 1
            await self._repository.append_timeline_event(
                result.organization_id,
                _timeline_values(
                    work_id=work_id,
                    event_type="dependency.created",
                    view=view,
                    correlation_id=result.correlation_id,
                    event_at=view.effective_event_time() or view.received_timestamp,
                    provenance=derivation.origin.value,
                ),
            )
            await self._enqueue(
                result,
                work_id=work_id,
                event_type=events_module.WorkEventType.DEPENDENCY_IDENTIFIED,
                payload={
                    "dependency_id": str(dependency_id),
                    "relationship": dep.relationship.value,
                },
                suffix=f"dependency:{dependency_id}",
            )
            # An open dependency that blocks or waits on something is a blocker candidate.
            candidate = blockers_module.blocker_from_dependency(
                relationship=dep.relationship,
                dependency_status="unknown",
                target_ref=dep.target_entity_ref,
                evidence=[{"dependency_id": str(dependency_id)}],
                detected_at=as_of,
            )
            if candidate is not None:
                blocker_id = await self._repository.upsert_blocker(
                    result.organization_id,
                    {
                        "affected_work_id": work_id,
                        "blocker_type": candidate.blocker_type,
                        "description": candidate.description,
                        "blocking_entity": candidate.blocking_entity,
                        "responsible_department_id": candidate.responsible_department_id,
                        "detected_at": as_of,
                        "expected_resolution_at": candidate.expected_resolution_at,
                        "blocker_status": candidate.status,
                        "evidence_references": candidate.evidence_references,
                        "provenance": candidate.provenance,
                        "verification_status": candidate.verification_status,
                        "correlation_id": result.correlation_id,
                    },
                )
                result.blockers_identified += 1
                await self._repository.append_timeline_event(
                    result.organization_id,
                    _timeline_values(
                        work_id=work_id,
                        event_type="blocker.identified",
                        view=view,
                        correlation_id=result.correlation_id,
                        event_at=view.effective_event_time() or view.received_timestamp,
                        provenance=candidate.provenance.value,
                    ),
                )
                await self._enqueue(
                    result,
                    work_id=work_id,
                    event_type=events_module.WorkEventType.WORK_BLOCKED,
                    payload={
                        "blocker_id": str(blocker_id),
                        "blocker_type": candidate.blocker_type.value,
                    },
                    suffix=f"blocker:{blocker_id}",
                )

        await self._compute_priority(result, work_id=work_id, derivation=derivation, as_of=as_of)
        await self._evaluate_followups(result, work_id=work_id, derivation=derivation, as_of=as_of)

    async def _compute_priority(
        self,
        result: WorkRunResult,
        *,
        work_id: uuid.UUID,
        derivation: WorkDerivation,
        as_of: datetime,
    ) -> None:
        overdue = is_overdue(due_at=derivation.due_at, status=derivation.status.value, as_of=as_of)
        due_soon = is_due_soon(
            due_at=derivation.due_at,
            status=derivation.status.value,
            as_of=as_of,
            within_hours=self._policy.due_soon_hours,
        )
        blocked = derivation.status in {WorkStatus.BLOCKED, WorkStatus.WAITING}
        dependency_count = len(derivation.dependencies)
        completeness = compute_source_completeness(
            {
                "title": derivation.title,
                "domain": derivation.domain,
                "status": derivation.status,
                "work_type": derivation.work_type,
                "owner_id": derivation.owner_id,
                "due_at": derivation.due_at,
                "department_id": derivation.department_id,
            }
        )
        inputs = PriorityInputs(
            overdue=overdue,
            due_soon=due_soon,
            blocked=blocked,
            dependency_count=dependency_count,
            source_completeness=completeness,
            source_priority=derivation.priority,
        )
        rule = derive_rule_priority(inputs)
        await self._repository.upsert_priority(
            result.organization_id,
            {
                "work_id": work_id,
                "level": rule.level,
                "source": "rule",
                "overdue": overdue,
                "due_soon": due_soon,
                "blocked": blocked,
                "dependency_count": dependency_count,
                "waiting_duration_seconds": None,
                "commitment_status": None,
                "source_completeness": completeness,
                "related_detection_ids": [],
            },
        )
        if overdue:
            await self._enqueue(
                result,
                work_id=work_id,
                event_type=events_module.WorkEventType.WORK_OVERDUE,
                payload={"due_at": derivation.due_at.isoformat() if derivation.due_at else None},
                suffix="overdue",
            )
        elif due_soon:
            await self._enqueue(
                result,
                work_id=work_id,
                event_type=events_module.WorkEventType.DEADLINE_APPROACHING,
                payload={"due_at": derivation.due_at.isoformat() if derivation.due_at else None},
                suffix="due-soon",
            )

    async def _evaluate_followups(
        self,
        result: WorkRunResult,
        *,
        work_id: uuid.UUID,
        derivation: WorkDerivation,
        as_of: datetime,
    ) -> None:
        last_activity = derivation.start_at
        inputs = FollowUpInputs(
            related_work_id=work_id,
            target_type=_followup_target(derivation),
            target_ref=_followup_target_ref(derivation),
            last_activity_at=last_activity,
            response_due_at=derivation.due_at if derivation.due_source is not None else None,
            work_status=derivation.status.value,
            blocked_since=last_activity if derivation.status == WorkStatus.BLOCKED else None,
            as_of=as_of,
        )
        candidates = evaluate_followups(
            inputs,
            response_window_hours=self._policy.response_window_hours,
            due_soon_hours=self._policy.due_soon_hours,
            unresolved_window_hours=self._policy.unresolved_window_hours,
        )
        for candidate in candidates:
            follow_up_id = await self._repository.upsert_followup(
                result.organization_id,
                {
                    "related_work_id": work_id,
                    "target_type": candidate.target_type,
                    "target_ref": candidate.target_ref,
                    "owner_id": derivation.owner_id,
                    "reason": candidate.reason,
                    "last_activity_at": candidate.last_activity_at,
                    "next_due_at": candidate.next_due_at,
                    "urgency": candidate.urgency,
                    "follow_up_status": candidate.status,
                    "rule_id": candidate.rule_id,
                    "provenance": "rule",
                    "correlation_id": result.correlation_id,
                },
            )
            result.followups_identified += 1
            await self._enqueue(
                result,
                work_id=work_id,
                event_type=events_module.WorkEventType.FOLLOWUP_REQUIRED,
                payload={"follow_up_id": str(follow_up_id), "rule_id": candidate.rule_id},
                suffix=f"followup:{candidate.rule_id}",
            )

    async def _associate_detections(
        self, organization_id: uuid.UUID, *, result: WorkRunResult, as_of: datetime
    ) -> None:
        """Associate Layer 3 detections with work that references the same machine.

        Association requires a verified reference: the work's ``machine_ref`` must equal the
        detection's ``entity_id``. The engine records the association on the work priority's
        ``related_detection_ids`` and never declares a cause.
        """
        detections = await self._repository.read_detections_window(
            organization_id, since=as_of - timedelta(hours=self._policy.unresolved_window_hours)
        )
        if not detections:
            return
        by_entity: dict[str, list[uuid.UUID]] = {}
        for detection in detections:
            by_entity.setdefault(detection.entity_id, []).append(detection.detection_id)

        rows = await self._repository.list_work_by_machine_refs(
            organization_id, machine_refs=tuple(by_entity)
        )
        for row in rows:
            detection_ids = by_entity.get(row.machine_ref or "", [])
            if not detection_ids:
                continue
            await self._repository.upsert_priority(
                organization_id,
                {
                    "work_id": row.id,
                    "level": row.priority,
                    "source": "rule",
                    "overdue": False,
                    "due_soon": False,
                    "blocked": row.status in {WorkStatus.BLOCKED, WorkStatus.WAITING},
                    "dependency_count": 0,
                    "waiting_duration_seconds": None,
                    "commitment_status": None,
                    "source_completeness": 0.0,
                    "related_detection_ids": detection_ids[:20],
                },
            )
            result.detections_associated += 1

    async def _enqueue(
        self,
        result: WorkRunResult,
        *,
        work_id: uuid.UUID,
        event_type: str,
        payload: dict[str, object],
        suffix: str,
    ) -> None:
        draft = events_module.WorkEventDraft(
            event_type=event_type,
            work_id=work_id,
            payload=payload,
            correlation_id=result.correlation_id,
        )
        await self._repository.enqueue_event(
            result.organization_id,
            {
                "work_id": work_id,
                "event_type": event_type,
                "idempotency_key": draft.idempotency_key(suffix=suffix),
                "payload": payload,
                "correlation_id": result.correlation_id,
            },
        )
        result.events_enqueued += 1


# --- Helpers --------------------------------------------------------------------


def _to_view(record: CanonicalRecord) -> SourceRecordView:
    return SourceRecordView(
        canonical_record_id=record.id,
        raw_record_id=record.raw_record_id,
        organization_id=record.organization_id,
        connection_id=record.connection_id,
        record_type=record.record_type,
        entity_type=record.entity_type,
        source_record_key=record.source_record_key,
        source_timestamp=record.source_timestamp,
        event_timestamp=record.event_timestamp,
        observed_timestamp=record.observed_timestamp,
        received_timestamp=record.received_timestamp,
        normalized_payload=record.normalized_payload or {},
        extensions=record.extensions or {},
        record_metadata=record.record_metadata or {},
        source_system=_source_system(record),
        correlation_id=record.correlation_id,
    )


def _source_system(record: CanonicalRecord) -> str:
    metadata = record.record_metadata or {}
    value = metadata.get("source_system")
    if isinstance(value, str) and value:
        return value[:120]
    return f"connection:{record.connection_id}"


def _entity_values(derivation: WorkDerivation, view: SourceRecordView) -> dict[str, object]:
    return {
        "work_key": derivation.work_key,
        "work_type": derivation.work_type,
        "title": derivation.title,
        "description": derivation.description,
        "domain": derivation.domain,
        "department_id": derivation.department_id,
        "owner_id": derivation.owner_id,
        "assigned_user_id": derivation.assigned_user_id,
        "priority": derivation.priority,
        "priority_source": derivation.priority_source,
        "status": derivation.status,
        "source_status": derivation.source_status,
        "origin": derivation.origin,
        "confidence": derivation.confidence,
        "start_at": derivation.start_at,
        "due_at": derivation.due_at,
        "due_source": derivation.due_source,
        "customer_ref": derivation.customer_ref,
        "supplier_ref": derivation.supplier_ref,
        "order_ref": derivation.order_ref,
        "project_ref": derivation.project_ref,
        "machine_ref": derivation.machine_ref,
        "production_ref": derivation.production_ref,
        "attributes": derivation.attributes,
        "correlation_id": view.correlation_id,
        "schema_version": WORK_SCHEMA_VERSION,
    }


def _timeline_values(
    *,
    work_id: uuid.UUID,
    event_type: str,
    view: SourceRecordView,
    correlation_id: str,
    event_at: datetime,
    provenance: str,
) -> dict[str, object]:
    now = datetime.now(tz=UTC)
    return {
        "work_id": work_id,
        "event_type": event_type,
        "event_at": event_at,
        "source_timestamp": view.source_timestamp,
        "ingested_at": now,
        "processed_at": now,
        "actor_type": "system",
        "source_reference": {
            "canonical_record_id": str(view.canonical_record_id),
            "source_record_key": view.source_record_key,
            "source_system": view.source_system,
        },
        "provenance": provenance,
        "correlation_id": correlation_id,
    }


def _followup_target(derivation: WorkDerivation) -> FollowUpTargetType:
    if derivation.supplier_ref:
        return FollowUpTargetType.SUPPLIER
    if derivation.customer_ref:
        return FollowUpTargetType.CUSTOMER
    if derivation.owner_id is not None:
        return FollowUpTargetType.PERSON
    return FollowUpTargetType.INTERNAL


def _followup_target_ref(derivation: WorkDerivation) -> str:
    return (
        derivation.supplier_ref
        or derivation.customer_ref
        or derivation.order_ref
        or derivation.work_key
    )[:300]


__all__ = [
    "DEFAULT_ROW_LIMIT",
    "DEFAULT_WINDOW_HOURS",
    "WorkIntelligenceEngine",
    "WorkRunResult",
    "WorkRunSkip",
]
