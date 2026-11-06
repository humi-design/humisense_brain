"""Layer 4 repository: the only place Layer 4 touches the database.

Every read and write runs inside a ``tenant_session``, so the Row-Level Security GUC is set
for the transaction and a query written without a tenant filter still cannot cross a tenant
boundary. The repository does not take a session from the caller: it opens its own, which
keeps tenant scoping impossible to forget.

Writes are idempotent on each table's natural key (``ON CONFLICT DO UPDATE``), so a re-run
over the same window upserts rather than duplicating. This is what makes scheduled and
backfill derivation safe to repeat.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from humisense.db.models import (
    CanonicalRecord,
    Department,
    DetectionEventRow,
    Organization,
    WorkAssignment,
    WorkBlocker,
    WorkCommitment,
    WorkConflict,
    WorkDependency,
    WorkEntity,
    WorkExtraction,
    WorkFollowUp,
    WorkOutboxEvent,
    WorkPriority,
    WorkSourceReference,
    WorkStatus,
    WorkTimelineEvent,
)
from humisense.db.session import Database


class WorkRepository:
    """Tenant-scoped persistence and reads for Layer 4."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- Discovery --------------------------------------------------------

    async def list_organization_ids(self) -> list[uuid.UUID]:
        """Return every tenant id. Used by the background sweep only."""
        async with self._database.system_session() as session:
            return list((await session.scalars(select(Organization.id))).all())

    async def read_canonical_window(
        self,
        organization_id: uuid.UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 5000,
    ) -> list[CanonicalRecord]:
        """Read canonical records in a bounded window, oldest first.

        Ordered by effective event time so a watermark advances monotonically. The window is
        bounded by ``limit`` so a run never loads an unbounded history.
        """
        async with self._database.tenant_session(organization_id) as session:
            statement = select(CanonicalRecord).where(
                CanonicalRecord.organization_id == organization_id
            )
            event_time = func.coalesce(
                CanonicalRecord.event_timestamp, CanonicalRecord.source_timestamp
            )
            if since is not None:
                statement = statement.where(event_time >= since)
            if until is not None:
                statement = statement.where(event_time <= until)
            statement = statement.order_by(event_time.asc().nulls_last()).limit(limit)
            return list((await session.scalars(statement)).all())

    async def read_detections_window(
        self,
        organization_id: uuid.UUID,
        *,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[DetectionEventRow]:
        """Read Layer 3 detections in a bounded window, newest first."""
        async with self._database.tenant_session(organization_id) as session:
            statement = select(DetectionEventRow).where(
                DetectionEventRow.organization_id == organization_id
            )
            if since is not None:
                statement = statement.where(DetectionEventRow.detected_at >= since)
            statement = statement.order_by(DetectionEventRow.detected_at.desc()).limit(limit)
            return list((await session.scalars(statement)).all())

    async def latest_work_watermark(self, organization_id: uuid.UUID) -> datetime | None:
        """Return the newest ``work_entities.updated_at`` for the tenant, or None."""
        async with self._database.tenant_session(organization_id) as session:
            value = await session.scalar(
                select(func.max(WorkEntity.updated_at)).where(
                    WorkEntity.organization_id == organization_id
                )
            )
        return value

    # --- Departments ------------------------------------------------------

    async def list_departments(self, organization_id: uuid.UUID) -> list[Department]:
        async with self._database.tenant_session(organization_id) as session:
            return list(
                (
                    await session.scalars(
                        select(Department)
                        .where(Department.organization_id == organization_id)
                        .order_by(Department.name)
                    )
                ).all()
            )

    async def ensure_department(
        self,
        organization_id: uuid.UUID,
        *,
        department_key: str,
        name: str,
        domain: str,
    ) -> uuid.UUID:
        """Upsert a department by its tenant-scoped key and return its id."""
        async with self._database.tenant_session(organization_id) as session:
            statement = (
                pg_insert(Department)
                .values(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    department_key=department_key,
                    name=name,
                    domain=domain,
                )
                .on_conflict_do_update(
                    index_elements=["organization_id", "department_key"],
                    set_={"name": name, "domain": domain},
                )
                .returning(Department.id)
            )
            return (await session.execute(statement)).scalar_one()

    # --- Work entities ----------------------------------------------------

    async def upsert_work_entity(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> tuple[uuid.UUID, bool]:
        """Upsert a work entity on its natural key.

        Returns ``(work_id, created)``. ``created`` uses PostgreSQL's ``xmax = 0`` test: a row
        produced by the INSERT path has ``xmax`` zero, a row produced by the DO UPDATE path
        does not. That is exact, whereas comparing ``created_at`` to ``updated_at`` only works
        while the two columns are guaranteed to differ, which is not a property the schema
        states.
        """
        async with self._database.tenant_session(organization_id) as session:
            insert_values = {**values, "organization_id": organization_id}
            if "id" not in insert_values:
                insert_values["id"] = uuid.uuid4()
            update_columns = {
                key: insert_values[key]
                for key in insert_values
                if key
                not in {
                    "id",
                    "organization_id",
                    "work_key",
                    "created_at",
                    "creator_id",
                    "origin",
                    "correlation_id",
                }
            }
            update_columns["updated_at"] = func.now()
            statement = (
                pg_insert(WorkEntity)
                .values(**insert_values)
                .on_conflict_do_update(
                    index_elements=["organization_id", "work_key"],
                    set_=update_columns,
                )
                .returning(WorkEntity.id, text("xmax = 0"))
            )
            work_id, created = (await session.execute(statement)).one()
            return work_id, bool(created)

    async def get_work_entity(
        self, organization_id: uuid.UUID, work_id: uuid.UUID
    ) -> WorkEntity | None:
        async with self._database.tenant_session(organization_id) as session:
            return cast(
                WorkEntity | None,
                await session.scalar(
                    select(WorkEntity).where(
                        WorkEntity.organization_id == organization_id, WorkEntity.id == work_id
                    )
                ),
            )

    async def get_work_entity_by_key(
        self, organization_id: uuid.UUID, work_key: str
    ) -> WorkEntity | None:
        async with self._database.tenant_session(organization_id) as session:
            return cast(
                WorkEntity | None,
                await session.scalar(
                    select(WorkEntity).where(
                        WorkEntity.organization_id == organization_id,
                        WorkEntity.work_key == work_key,
                    )
                ),
            )

    async def list_work_entities(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        work_type: str | None = None,
        domain: str | None = None,
        department_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        search: str | None = None,
    ) -> tuple[list[WorkEntity], int]:
        """Return a page of work entities and the total matching count."""
        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkEntity.organization_id == organization_id]
            if status is not None:
                conditions.append(WorkEntity.status == WorkStatus(status))
            if work_type is not None:
                conditions.append(WorkEntity.work_type == work_type)
            if domain is not None:
                conditions.append(WorkEntity.domain == domain)
            if department_id is not None:
                conditions.append(WorkEntity.department_id == department_id)
            if owner_id is not None:
                conditions.append(
                    or_(WorkEntity.owner_id == owner_id, WorkEntity.assigned_user_id == owner_id)
                )
            if due_before is not None:
                conditions.append(WorkEntity.due_at <= due_before)
            if due_after is not None:
                conditions.append(WorkEntity.due_at >= due_after)
            if search:
                # A bounded, parameterised ILIKE. The value is a bind parameter, never
                # interpolated into SQL, so a wildcard cannot become an injection.
                pattern = f"%{search[:100]}%"
                conditions.append(WorkEntity.title.ilike(pattern))

            total = await session.scalar(
                select(func.count()).select_from(WorkEntity).where(*conditions)
            )
            rows = list(
                (
                    await session.scalars(
                        select(WorkEntity)
                        .where(*conditions)
                        .order_by(WorkEntity.updated_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            return rows, int(total or 0)

    async def update_work_entity(
        self, organization_id: uuid.UUID, work_id: uuid.UUID, values: dict[str, Any]
    ) -> WorkEntity | None:
        async with self._database.tenant_session(organization_id) as session:
            row = await session.scalar(
                select(WorkEntity).where(
                    WorkEntity.organization_id == organization_id, WorkEntity.id == work_id
                )
            )
            if row is None:
                return None
            for key, value in values.items():
                setattr(row, key, value)
            await session.flush()
            return row

    async def list_work_by_machine_refs(
        self, organization_id: uuid.UUID, *, machine_refs: tuple[str, ...], limit: int = 500
    ) -> list[WorkEntity]:
        """Return work entities that reference any of the given machine identifiers.

        Used to associate Layer 3 detections with existing work. The join is on an explicit
        reference, never on text similarity.
        """
        if not machine_refs:
            return []
        async with self._database.tenant_session(organization_id) as session:
            return list(
                (
                    await session.scalars(
                        select(WorkEntity)
                        .where(
                            WorkEntity.organization_id == organization_id,
                            WorkEntity.machine_ref.in_(list(machine_refs)[:200]),
                        )
                        .limit(limit)
                    )
                ).all()
            )

    # --- Source references ------------------------------------------------

    async def upsert_source_reference(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> None:
        async with self._database.tenant_session(organization_id) as session:
            insert_values = {**values, "organization_id": organization_id, "id": uuid.uuid4()}
            statement = (
                pg_insert(WorkSourceReference)
                .values(**insert_values)
                .on_conflict_do_update(
                    index_elements=["organization_id", "work_id", "canonical_record_id"],
                    set_={
                        "relationship": insert_values["relationship"],
                        "confidence": insert_values.get("confidence"),
                    },
                )
            )
            await session.execute(statement)

    async def list_source_references(
        self, organization_id: uuid.UUID, work_id: uuid.UUID
    ) -> list[WorkSourceReference]:
        async with self._database.tenant_session(organization_id) as session:
            return list(
                (
                    await session.scalars(
                        select(WorkSourceReference)
                        .where(
                            WorkSourceReference.organization_id == organization_id,
                            WorkSourceReference.work_id == work_id,
                        )
                        .order_by(WorkSourceReference.created_at)
                    )
                ).all()
            )

    # --- Assignments ------------------------------------------------------

    async def upsert_assignment(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        """Insert an assignment if an equivalent one does not already exist.

        There is no natural unique key on this table because a user may hold several roles;
        the guard is an explicit existence check on the full identity of the assignment.
        """
        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(WorkAssignment.id).where(
                    WorkAssignment.organization_id == organization_id,
                    WorkAssignment.work_id == values["work_id"],
                    WorkAssignment.role == values["role"],
                    WorkAssignment.participant_type == values["participant_type"],
                    WorkAssignment.user_id.is_not_distinct_from(values.get("user_id")),
                    WorkAssignment.department_id.is_not_distinct_from(values.get("department_id")),
                    WorkAssignment.external_name.is_not_distinct_from(values.get("external_name")),
                )
            )
            if existing is None:
                session.add(WorkAssignment(**{**values, "organization_id": organization_id}))

    async def list_assignments(
        self, organization_id: uuid.UUID, work_id: uuid.UUID
    ) -> list[WorkAssignment]:
        async with self._database.tenant_session(organization_id) as session:
            return list(
                (
                    await session.scalars(
                        select(WorkAssignment)
                        .where(
                            WorkAssignment.organization_id == organization_id,
                            WorkAssignment.work_id == work_id,
                        )
                        .order_by(WorkAssignment.created_at)
                    )
                ).all()
            )

    # --- Timeline and status history --------------------------------------

    async def append_timeline_event(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> None:
        async with self._database.tenant_session(organization_id) as session:
            session.add(WorkTimelineEvent(**{**values, "organization_id": organization_id}))

    async def list_timeline(
        self, organization_id: uuid.UUID, work_id: uuid.UUID, *, limit: int = 200
    ) -> list[WorkTimelineEvent]:
        async with self._database.tenant_session(organization_id) as session:
            return list(
                (
                    await session.scalars(
                        select(WorkTimelineEvent)
                        .where(
                            WorkTimelineEvent.organization_id == organization_id,
                            WorkTimelineEvent.work_id == work_id,
                        )
                        .order_by(WorkTimelineEvent.event_at.desc())
                        .limit(limit)
                    )
                ).all()
            )

    async def append_status_history(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> None:
        from humisense.db.models import WorkStatusHistory

        async with self._database.tenant_session(organization_id) as session:
            session.add(WorkStatusHistory(**{**values, "organization_id": organization_id}))

    # --- Commitments ------------------------------------------------------

    async def upsert_commitment(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> tuple[uuid.UUID, dict[str, Any] | None]:
        """Upsert a commitment, returning its id and the previous state when it changed.

        The previous state is returned so the caller can append a history row. A no-op upsert
        (nothing changed) returns ``None``, which keeps the history honest.
        """
        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(WorkCommitment).where(
                    WorkCommitment.organization_id == organization_id,
                    WorkCommitment.commitment_key == values["commitment_key"],
                )
            )
            if existing is None:
                row = WorkCommitment(**{**values, "organization_id": organization_id})
                session.add(row)
                await session.flush()
                return row.id, None
            previous = {
                "commitment_status": existing.commitment_status.value,
                "due_at": existing.due_at,
            }
            changed = existing.commitment_status != values[
                "commitment_status"
            ] or existing.due_at != values.get("due_at")
            for key, value in values.items():
                if key not in {"id", "organization_id", "commitment_key", "created_at"}:
                    setattr(existing, key, value)
            await session.flush()
            return existing.id, previous if changed else None

    async def append_commitment_history(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> None:
        from humisense.db.models import WorkCommitmentHistory

        async with self._database.tenant_session(organization_id) as session:
            session.add(WorkCommitmentHistory(**{**values, "organization_id": organization_id}))

    async def list_commitments(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        work_id: uuid.UUID | None = None,
        commitment_status: str | None = None,
        commitment_type: str | None = None,
    ) -> tuple[list[WorkCommitment], int]:
        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkCommitment.organization_id == organization_id]
            if work_id is not None:
                conditions.append(WorkCommitment.work_id == work_id)
            if commitment_status is not None:
                conditions.append(WorkCommitment.commitment_status == commitment_status)
            if commitment_type is not None:
                conditions.append(WorkCommitment.commitment_type == commitment_type)
            total = await session.scalar(
                select(func.count()).select_from(WorkCommitment).where(*conditions)
            )
            rows = list(
                (
                    await session.scalars(
                        select(WorkCommitment)
                        .where(*conditions)
                        .order_by(WorkCommitment.due_at.asc().nulls_last())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            return rows, int(total or 0)

    async def get_commitment(
        self, organization_id: uuid.UUID, commitment_id: uuid.UUID
    ) -> WorkCommitment | None:
        async with self._database.tenant_session(organization_id) as session:
            return cast(
                WorkCommitment | None,
                await session.scalar(
                    select(WorkCommitment).where(
                        WorkCommitment.organization_id == organization_id,
                        WorkCommitment.id == commitment_id,
                    )
                ),
            )

    # --- Dependencies -----------------------------------------------------

    async def upsert_dependency(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> uuid.UUID:
        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(WorkDependency).where(
                    WorkDependency.organization_id == organization_id,
                    WorkDependency.source_work_id == values["source_work_id"],
                    WorkDependency.relationship == values["relationship"],
                    WorkDependency.target_work_id.is_not_distinct_from(
                        values.get("target_work_id")
                    ),
                    WorkDependency.target_entity_ref.is_not_distinct_from(
                        values.get("target_entity_ref")
                    ),
                )
            )
            if existing is not None:
                return existing.id
            row = WorkDependency(**{**values, "organization_id": organization_id})
            session.add(row)
            await session.flush()
            return row.id

    async def list_dependencies(
        self, organization_id: uuid.UUID, *, work_id: uuid.UUID | None = None, limit: int = 500
    ) -> list[WorkDependency]:
        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkDependency.organization_id == organization_id]
            if work_id is not None:
                conditions.append(
                    or_(
                        WorkDependency.source_work_id == work_id,
                        WorkDependency.target_work_id == work_id,
                    )
                )
            return list(
                (
                    await session.scalars(select(WorkDependency).where(*conditions).limit(limit))
                ).all()
            )

    # --- Blockers ---------------------------------------------------------

    async def upsert_blocker(self, organization_id: uuid.UUID, values: dict[str, Any]) -> uuid.UUID:
        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(WorkBlocker).where(
                    WorkBlocker.organization_id == organization_id,
                    WorkBlocker.affected_work_id == values["affected_work_id"],
                    WorkBlocker.blocker_type == values["blocker_type"],
                    WorkBlocker.description == values["description"],
                    WorkBlocker.blocker_status == values.get("blocker_status", "open"),
                )
            )
            if existing is not None:
                return existing.id
            row = WorkBlocker(**{**values, "organization_id": organization_id})
            session.add(row)
            await session.flush()
            return row.id

    async def list_blockers(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        work_id: uuid.UUID | None = None,
        blocker_status: str | None = None,
    ) -> tuple[list[WorkBlocker], int]:
        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkBlocker.organization_id == organization_id]
            if work_id is not None:
                conditions.append(WorkBlocker.affected_work_id == work_id)
            if blocker_status is not None:
                conditions.append(WorkBlocker.blocker_status == blocker_status)
            total = await session.scalar(
                select(func.count()).select_from(WorkBlocker).where(*conditions)
            )
            rows = list(
                (
                    await session.scalars(
                        select(WorkBlocker)
                        .where(*conditions)
                        .order_by(WorkBlocker.detected_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            return rows, int(total or 0)

    # --- Follow-ups -------------------------------------------------------

    async def upsert_followup(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> uuid.UUID:
        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(WorkFollowUp).where(
                    WorkFollowUp.organization_id == organization_id,
                    WorkFollowUp.related_work_id == values["related_work_id"],
                    WorkFollowUp.rule_id == values["rule_id"],
                    WorkFollowUp.target_ref == values["target_ref"],
                )
            )
            if existing is not None:
                for key in ("next_due_at", "urgency", "follow_up_status", "last_activity_at"):
                    if key in values:
                        setattr(existing, key, values[key])
                await session.flush()
                return existing.id
            row = WorkFollowUp(**{**values, "organization_id": organization_id})
            session.add(row)
            await session.flush()
            return row.id

    async def list_followups(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        work_id: uuid.UUID | None = None,
        follow_up_status: str | None = None,
        target_type: str | None = None,
    ) -> tuple[list[WorkFollowUp], int]:
        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkFollowUp.organization_id == organization_id]
            if work_id is not None:
                conditions.append(WorkFollowUp.related_work_id == work_id)
            if follow_up_status is not None:
                conditions.append(WorkFollowUp.follow_up_status == follow_up_status)
            if target_type is not None:
                conditions.append(WorkFollowUp.target_type == target_type)
            total = await session.scalar(
                select(func.count()).select_from(WorkFollowUp).where(*conditions)
            )
            rows = list(
                (
                    await session.scalars(
                        select(WorkFollowUp)
                        .where(*conditions)
                        .order_by(WorkFollowUp.next_due_at.asc().nulls_last())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            return rows, int(total or 0)

    # --- Priorities -------------------------------------------------------

    async def upsert_priority(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            insert_values = {**values, "organization_id": organization_id, "id": uuid.uuid4()}
            # ``related_detection_ids`` holds UUIDs, and the PostgreSQL JSONB type does not
            # serialize a UUID. Serializing here keeps the cast explicit and stops a raw
            # UUID from reaching the driver as an unserializable object.
            if insert_values.get("related_detection_ids") is not None:
                insert_values["related_detection_ids"] = json.loads(
                    json.dumps(insert_values["related_detection_ids"], default=str)
                )
            statement = (
                pg_insert(WorkPriority)
                .values(**insert_values)
                .on_conflict_do_update(
                    index_elements=["organization_id", "work_id", "source"],
                    set_={
                        key: insert_values[key]
                        for key in insert_values
                        if key not in {"id", "organization_id", "work_id", "source", "computed_at"}
                    }
                    | {"computed_at": func.now()},
                )
            )
            await session.execute(statement)

    async def get_priority(
        self, organization_id: uuid.UUID, work_id: uuid.UUID, *, source: str
    ) -> WorkPriority | None:
        async with self._database.tenant_session(organization_id) as session:
            return cast(
                WorkPriority | None,
                await session.scalar(
                    select(WorkPriority).where(
                        WorkPriority.organization_id == organization_id,
                        WorkPriority.work_id == work_id,
                        WorkPriority.source == source,
                    )
                ),
            )

    async def list_priorities(
        self, organization_id: uuid.UUID, work_id: uuid.UUID
    ) -> list[WorkPriority]:
        """Return every priority record for a work item, one per source.

        The API returns all of them side by side so a rule-derived level is never presented
        as if it had replaced a human one.
        """
        async with self._database.tenant_session(organization_id) as session:
            return list(
                (
                    await session.scalars(
                        select(WorkPriority).where(
                            WorkPriority.organization_id == organization_id,
                            WorkPriority.work_id == work_id,
                        )
                    )
                ).all()
            )

    # --- Conflicts --------------------------------------------------------

    async def upsert_conflict(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            insert_values = {**values, "organization_id": organization_id, "id": uuid.uuid4()}
            statement = (
                pg_insert(WorkConflict)
                .values(**insert_values)
                .on_conflict_do_nothing(
                    index_elements=["organization_id", "entity_type", "entity_key", "conflict_type"]
                )
            )
            await session.execute(statement)

    async def list_conflicts(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        conflict_type: str | None = None,
    ) -> tuple[list[WorkConflict], int]:
        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkConflict.organization_id == organization_id]
            if status is not None:
                conditions.append(WorkConflict.status == status)
            if conflict_type is not None:
                conditions.append(WorkConflict.conflict_type == conflict_type)
            total = await session.scalar(
                select(func.count()).select_from(WorkConflict).where(*conditions)
            )
            rows = list(
                (
                    await session.scalars(
                        select(WorkConflict)
                        .where(*conditions)
                        .order_by(WorkConflict.detected_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            return rows, int(total or 0)

    async def resolve_conflict(
        self,
        organization_id: uuid.UUID,
        conflict_id: uuid.UUID,
        *,
        resolved_value: dict[str, Any] | None,
        resolved_by: uuid.UUID,
        resolution_note: str | None,
        status: str,
    ) -> WorkConflict | None:
        async with self._database.tenant_session(organization_id) as session:
            row = await session.scalar(
                select(WorkConflict).where(
                    WorkConflict.organization_id == organization_id,
                    WorkConflict.id == conflict_id,
                )
            )
            if row is None:
                return None
            row.resolved_value = resolved_value
            row.resolved_by = resolved_by
            row.resolution_note = resolution_note
            row.status = status  # type: ignore[assignment]
            row.resolved_at = datetime.now(tz=UTC)
            await session.flush()
            return row

    # --- Extractions ------------------------------------------------------

    async def upsert_extraction(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._database.tenant_session(organization_id) as session:
            insert_values = {**values, "organization_id": organization_id, "id": uuid.uuid4()}
            statement = (
                pg_insert(WorkExtraction)
                .values(**insert_values)
                .on_conflict_do_update(
                    index_elements=["organization_id", "extraction_key"],
                    set_={
                        "fields": insert_values["fields"],
                        "confidence": insert_values["confidence"],
                        "verification_status": insert_values["verification_status"],
                    },
                )
            )
            await session.execute(statement)

    async def list_extractions(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        verification_status: str | None = None,
    ) -> tuple[list[WorkExtraction], int]:
        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkExtraction.organization_id == organization_id]
            if verification_status is not None:
                conditions.append(WorkExtraction.verification_status == verification_status)
            total = await session.scalar(
                select(func.count()).select_from(WorkExtraction).where(*conditions)
            )
            rows = list(
                (
                    await session.scalars(
                        select(WorkExtraction)
                        .where(*conditions)
                        .order_by(WorkExtraction.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            return rows, int(total or 0)

    # --- Workflows --------------------------------------------------------

    async def ensure_workflow_definition(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> uuid.UUID:
        """Create a workflow definition version if it does not already exist."""
        from humisense.db.models import WorkflowDefinition

        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(WorkflowDefinition.id).where(
                    WorkflowDefinition.organization_id == organization_id,
                    WorkflowDefinition.workflow_key == values["workflow_key"],
                    WorkflowDefinition.version == values.get("version", 1),
                )
            )
            if existing is not None:
                return existing
            row = WorkflowDefinition(**{**values, "organization_id": organization_id})
            session.add(row)
            await session.flush()
            return row.id

    async def upsert_workflow_instance(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> uuid.UUID:
        from humisense.db.models import WorkflowInstance

        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(WorkflowInstance).where(
                    WorkflowInstance.organization_id == organization_id,
                    WorkflowInstance.definition_id == values["definition_id"],
                    WorkflowInstance.work_id == values["work_id"],
                )
            )
            if existing is not None:
                for key in ("status", "current_step_key"):
                    if key in values:
                        setattr(existing, key, values[key])
                await session.flush()
                return existing.id
            row = WorkflowInstance(**{**values, "organization_id": organization_id})
            session.add(row)
            await session.flush()
            return row.id

    async def upsert_workflow_step(
        self, organization_id: uuid.UUID, values: dict[str, Any]
    ) -> None:
        from humisense.db.models import WorkflowStep

        async with self._database.tenant_session(organization_id) as session:
            existing = await session.scalar(
                select(WorkflowStep).where(
                    WorkflowStep.organization_id == organization_id,
                    WorkflowStep.instance_id == values["instance_id"],
                    WorkflowStep.step_key == values["step_key"],
                )
            )
            if existing is not None:
                for key in ("observed_state", "step_name", "step_order", "depends_on"):
                    if key in values:
                        setattr(existing, key, values[key])
                await session.flush()
                return
            session.add(WorkflowStep(**{**values, "organization_id": organization_id}))

    async def list_workflow_instances(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        work_id: uuid.UUID | None = None,
    ) -> list[tuple[Any, Any, list[Any]]]:
        """Return instances with their definition and steps."""
        from humisense.db.models import WorkflowDefinition, WorkflowInstance, WorkflowStep

        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkflowInstance.organization_id == organization_id]
            if work_id is not None:
                conditions.append(WorkflowInstance.work_id == work_id)
            instances = list(
                (
                    await session.scalars(
                        select(WorkflowInstance)
                        .where(*conditions)
                        .order_by(WorkflowInstance.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            results: list[tuple[Any, Any, list[Any]]] = []
            for instance in instances:
                definition = await session.scalar(
                    select(WorkflowDefinition).where(
                        WorkflowDefinition.organization_id == organization_id,
                        WorkflowDefinition.id == instance.definition_id,
                    )
                )
                steps = list(
                    (
                        await session.scalars(
                            select(WorkflowStep)
                            .where(
                                WorkflowStep.organization_id == organization_id,
                                WorkflowStep.instance_id == instance.id,
                            )
                            .order_by(WorkflowStep.step_order)
                        )
                    ).all()
                )
                results.append((instance, definition, steps))
            return results

    async def count_workflow_instances(
        self, organization_id: uuid.UUID, *, work_id: uuid.UUID | None = None
    ) -> int:
        from humisense.db.models import WorkflowInstance

        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkflowInstance.organization_id == organization_id]
            if work_id is not None:
                conditions.append(WorkflowInstance.work_id == work_id)
            count = await session.scalar(
                select(func.count()).select_from(WorkflowInstance).where(*conditions)
            )
        return int(count or 0)

    # --- Outbox -----------------------------------------------------------
    async def enqueue_event(self, organization_id: uuid.UUID, values: dict[str, Any]) -> None:
        """Insert an outbox event, ignoring a duplicate idempotency key."""
        async with self._database.tenant_session(organization_id) as session:
            insert_values = {**values, "organization_id": organization_id, "id": uuid.uuid4()}
            statement = (
                pg_insert(WorkOutboxEvent)
                .values(**insert_values)
                .on_conflict_do_nothing(index_elements=["organization_id", "idempotency_key"])
            )
            await session.execute(statement)

    async def claim_pending_events(
        self, organization_id: uuid.UUID, *, limit: int = 100
    ) -> list[WorkOutboxEvent]:
        """Lock and return pending events for publication."""
        from humisense.db.models import WorkEventStatus

        async with self._database.tenant_session(organization_id) as session:
            return list(
                (
                    await session.scalars(
                        select(WorkOutboxEvent)
                        .where(
                            WorkOutboxEvent.organization_id == organization_id,
                            WorkOutboxEvent.status == WorkEventStatus.PENDING,
                        )
                        .order_by(WorkOutboxEvent.created_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )

    # --- Aggregation for views -------------------------------------------

    async def status_counts(
        self, organization_id: uuid.UUID, *, department_id: uuid.UUID | None = None
    ) -> dict[str, int]:
        async with self._database.tenant_session(organization_id) as session:
            conditions = [WorkEntity.organization_id == organization_id]
            if department_id is not None:
                conditions.append(WorkEntity.department_id == department_id)
            rows = (
                await session.execute(
                    select(WorkEntity.status, func.count())
                    .where(*conditions)
                    .group_by(WorkEntity.status)
                )
            ).all()
        return {row[0].value: int(row[1]) for row in rows}

    async def overdue_count(
        self, organization_id: uuid.UUID, *, as_of: datetime, department_id: uuid.UUID | None = None
    ) -> int:
        async with self._database.tenant_session(organization_id) as session:
            conditions = [
                WorkEntity.organization_id == organization_id,
                WorkEntity.due_at.is_not(None),
                WorkEntity.due_at < as_of,
                WorkEntity.status.not_in([WorkStatus.COMPLETED, WorkStatus.CANCELLED]),
            ]
            if department_id is not None:
                conditions.append(WorkEntity.department_id == department_id)
            count = await session.scalar(
                select(func.count()).select_from(WorkEntity).where(*conditions)
            )
        return int(count or 0)


__all__ = ["WorkRepository"]
