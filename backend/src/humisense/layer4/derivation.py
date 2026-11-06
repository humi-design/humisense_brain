"""Deterministic derivation of work entities from verified source records.

This is the heart of Layer 4. It turns a Layer 2 canonical record into a work-entity draft,
its source reference, its timeline event, and any commitments or dependencies the record
supports.

What it does **not** do is as important as what it does:

* It never invents an owner, a due date, a department, or a status. Each comes from an
  explicit source field or is left unresolved.
* It never treats a mentioned name as an assignment.
* It never treats a generic date as a deadline. Only an explicit due/required/committed key
  becomes one, and its type is recorded.
* It never promotes an inferred value to ``EXPLICIT``. The origin and confidence travel with
  the derived work.
* It never executes or interprets source text. Extracted and mentioned values are data.

Every derived value carries the rule that produced it, so a reviewer can reproduce it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from humisense.db.models import (
    CommitmentStatus,
    CommitmentType,
    DeadlineType,
    DependencyRelationship,
    DependencyTargetType,
    PriorityLevel,
    PrioritySource,
    SourceReferenceRelationship,
    VerificationStatus,
    WorkDomain,
    WorkOrigin,
    WorkStatus,
    WorkType,
)
from humisense.layer4 import ownership
from humisense.layer4.contract import WORK_PROCESSING_VERSION, WORK_SCHEMA_VERSION
from humisense.layer4.departments import DepartmentMapping, resolve_domain
from humisense.layer4.errors import WorkDerivationError
from humisense.layer4.policy import DUE_DATE_KEYS, WorkPolicy
from humisense.layer4.status import SourceStatusMapping

DERIVATION_TASK = "task_from_record"
DERIVATION_COMMITMENT = "commitment_from_record"


@dataclass(frozen=True, slots=True)
class SourceRecordView:
    """The parts of a Layer 2 canonical record that derivation reads.

    A narrow view keeps derivation testable without constructing a full ORM row, and makes
    explicit exactly which fields Layer 4 depends on.
    """

    canonical_record_id: uuid.UUID
    raw_record_id: uuid.UUID | None
    organization_id: uuid.UUID
    connection_id: uuid.UUID
    record_type: str
    entity_type: str | None
    source_record_key: str
    source_timestamp: datetime | None
    event_timestamp: datetime | None
    observed_timestamp: datetime
    received_timestamp: datetime
    normalized_payload: dict[str, Any]
    extensions: dict[str, Any]
    record_metadata: dict[str, Any]
    source_system: str
    correlation_id: str

    def effective_event_time(self) -> datetime | None:
        """Return the source's best event time, never a processing time."""
        return self.event_timestamp or self.source_timestamp

    def payload(self) -> dict[str, Any]:
        """Return the flattened payload: normalized fields plus preserved extensions.

        The Layer 2 business normalizer keeps ``{record_kind, identifier, text, party}`` in
        ``normalized_payload`` and preserves every other source field in ``extensions``.
        Derivation reads both, but only through explicit keys.
        """
        merged: dict[str, Any] = dict(self.extensions)
        merged.update(self.normalized_payload)
        return merged


@dataclass(slots=True)
class CommitmentDraft:
    commitment_key: str
    commitment_type: CommitmentType
    description: str
    committing_party: str
    receiving_party: str
    commitment_status: CommitmentStatus
    verification_status: VerificationStatus
    confidence: float | None
    due_at: datetime | None = None
    related_customer_ref: str | None = None
    related_supplier_ref: str | None = None
    related_order_ref: str | None = None
    source_reference: str | None = None


@dataclass(slots=True)
class DependencyDraft:
    relationship: DependencyRelationship
    target_entity_type: DependencyTargetType
    target_entity_ref: str | None = None
    verification_status: VerificationStatus = VerificationStatus.UNRESOLVED
    origin: WorkOrigin = WorkOrigin.INFERRED


@dataclass(slots=True)
class WorkDerivation:
    """Everything one source record derives into."""

    work_key: str
    work_type: WorkType
    title: str
    domain: WorkDomain
    status: WorkStatus
    origin: WorkOrigin
    confidence: float | None
    priority: PriorityLevel
    priority_source: PrioritySource
    source_status: str | None
    department_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    assigned_user_id: uuid.UUID | None
    description: str | None = None
    due_at: datetime | None = None
    due_source: DeadlineType | None = None
    start_at: datetime | None = None
    customer_ref: str | None = None
    supplier_ref: str | None = None
    order_ref: str | None = None
    project_ref: str | None = None
    machine_ref: str | None = None
    production_ref: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    source_relationship: SourceReferenceRelationship = SourceReferenceRelationship.PRIMARY
    assignments: list[ownership.ResolvedAssignment] = field(default_factory=list)
    commitments: list[CommitmentDraft] = field(default_factory=list)
    dependencies: list[DependencyDraft] = field(default_factory=list)
    mentioned: list[str] = field(default_factory=list)
    derivations: list[str] = field(default_factory=list)


def work_key_for(*, work_type: WorkType, source_system: str, source_record_key: str) -> str:
    """Return the deterministic, tenant-scoped identity key for source-derived work.

    Stable across re-runs, so re-deriving the same record upserts rather than duplicates.
    """
    return f"{work_type.value}:{source_system}:{source_record_key}"[:300]


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a source date value to an aware datetime, or None.

    Accepts an ISO-8601 string or a datetime. A naive value is refused rather than assumed
    to be UTC, because assuming would silently shift every deadline.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)
    return None


def resolve_due_date(payload: dict[str, Any]) -> tuple[datetime | None, DeadlineType | None]:
    """Return the first explicit due date and its type, or ``(None, None)``.

    Only keys that name a due/required/committed date are consulted. A generic ``date`` or a
    meeting time is deliberately ignored: treating every date as a deadline is exactly the
    error the brief warns against.
    """
    for key, deadline_type in DUE_DATE_KEYS:
        candidate = _as_datetime(payload.get(key))
        if candidate is not None:
            return candidate, deadline_type
    return None, None


def _title_for(payload: dict[str, Any], *, record_type: str, source_record_key: str) -> str:
    for key in ("title", "subject", "name", "summary", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return f"{record_type} {source_record_key}"[:300]


def _description_for(payload: dict[str, Any]) -> str | None:
    for key in ("description", "body", "text", "details", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _party_ref(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    value = _first_present(payload, keys)
    if value is None:
        return None
    return str(value)[:200]


def _commitment_type_for(payload: dict[str, Any], work_type: WorkType) -> CommitmentType | None:
    """Determine a commitment type from an explicit field, or from the work type.

    Returns None when nothing supports a commitment, so a plain task never becomes one.
    """
    explicit = _first_present(payload, ("commitment_type",))
    if explicit is not None:
        try:
            return CommitmentType(str(explicit).strip().lower())
        except ValueError:
            return None
    if work_type == WorkType.SUPPLIER_COMMITMENT:
        return CommitmentType.SUPPLIER
    if work_type == WorkType.CUSTOMER_COMMITMENT:
        return CommitmentType.CUSTOMER
    if work_type == WorkType.DELIVERY_OBLIGATION:
        return CommitmentType.DELIVERY
    return None


def _commitment_status_for(payload: dict[str, Any]) -> CommitmentStatus:
    """Map an explicit commitment status, defaulting to PROPOSED.

    The default matters: a date the source merely mentions is *proposed*, never confirmed.
    """
    explicit = _first_present(payload, ("commitment_status", "promise_status"))
    if explicit is None:
        return CommitmentStatus.PROPOSED
    try:
        return CommitmentStatus(str(explicit).strip().lower())
    except ValueError:
        return CommitmentStatus.PROPOSED


def _priority_for(payload: dict[str, Any]) -> tuple[PriorityLevel, PrioritySource]:
    explicit = _first_present(payload, ("priority", "severity"))
    if explicit is None:
        return PriorityLevel.NORMAL, PrioritySource.SOURCE
    text = str(explicit).strip().lower()
    mapping = {
        "low": PriorityLevel.LOW,
        "normal": PriorityLevel.NORMAL,
        "medium": PriorityLevel.NORMAL,
        "high": PriorityLevel.HIGH,
        "urgent": PriorityLevel.URGENT,
        "critical": PriorityLevel.CRITICAL,
    }
    if text in mapping:
        return mapping[text], PrioritySource.SOURCE
    return PriorityLevel.NORMAL, PrioritySource.SOURCE


def _dependency_drafts(payload: dict[str, Any]) -> list[DependencyDraft]:
    """Derive dependencies from explicit dependency fields.

    Only explicit fields produce a dependency. Text is never scanned, and an explicit edge is
    marked ``SOURCE_REPORTED`` rather than ``VERIFIED`` because Layer 4 has not confirmed the
    target exists.
    """
    drafts: list[DependencyDraft] = []
    blocked_by = payload.get("blocked_by")
    if isinstance(blocked_by, list):
        for ref in blocked_by[:20]:
            drafts.append(
                DependencyDraft(
                    relationship=DependencyRelationship.BLOCKED_BY,
                    target_entity_type=DependencyTargetType.WORK,
                    target_entity_ref=str(ref)[:300],
                    verification_status=VerificationStatus.SOURCE_REPORTED,
                    origin=WorkOrigin.EXPLICIT,
                )
            )
    waiting_for = payload.get("waiting_for")
    if isinstance(waiting_for, str) and waiting_for.strip():
        drafts.append(
            DependencyDraft(
                relationship=DependencyRelationship.WAITING_FOR,
                target_entity_type=DependencyTargetType.EXTERNAL_MILESTONE,
                target_entity_ref=waiting_for.strip()[:300],
                verification_status=VerificationStatus.SOURCE_REPORTED,
                origin=WorkOrigin.EXPLICIT,
            )
        )
    material = payload.get("material_dependency")
    if isinstance(material, str) and material.strip():
        drafts.append(
            DependencyDraft(
                relationship=DependencyRelationship.MATERIAL_DEPENDENCY,
                target_entity_type=DependencyTargetType.MATERIAL,
                target_entity_ref=material.strip()[:300],
                verification_status=VerificationStatus.SOURCE_REPORTED,
                origin=WorkOrigin.EXPLICIT,
            )
        )
    return drafts


async def derive_work(
    record: SourceRecordView,
    *,
    policy: WorkPolicy,
    status_mapping: SourceStatusMapping,
    department_mapping: DepartmentMapping | None = None,
    directory: ownership.UserDirectory | None = None,
) -> WorkDerivation | None:
    """Derive a work entity from one canonical record, or return None.

    Returning None means the record does not describe work under this tenant's policy — for
    example an email or a document that the tenant has not configured as a work source. It is
    not an error; it is the honest answer.
    """
    record_type = record.record_type
    if not policy.is_derivable(record_type):
        return None

    payload = record.payload()
    if not payload:
        raise WorkDerivationError(
            detail=f"record {record.canonical_record_id} has no payload to derive from"
        )

    work_type = policy.work_type_for(record_type)
    origin_text = policy.origin_for(record_type)
    origin = WorkOrigin(origin_text)

    status = status_mapping.map(
        source_system=record.source_system,
        source_status=_source_status(payload),
    )
    due_at, due_source = resolve_due_date(payload)
    priority, priority_source = _priority_for(payload)

    domain = resolve_domain(payload, record_kind=(record.entity_type or record_type))
    department_id = None
    if department_mapping is not None:
        department_id = department_mapping.resolve_department(
            payload, record_kind=(record.entity_type or record_type)
        )

    owner = await ownership.resolve_owner(
        organization_id=record.organization_id, payload=payload, directory=directory
    )
    requester = await ownership.resolve_requester(
        organization_id=record.organization_id, payload=payload, directory=directory
    )

    assignments: list[ownership.ResolvedAssignment] = []
    owner_id: uuid.UUID | None = None
    assigned_user_id: uuid.UUID | None = None
    if owner is not None:
        assignments.append(owner)
        if owner.participant_type.value == "user":
            owner_id = owner.user_id
            assigned_user_id = owner.user_id
    if requester is not None:
        assignments.append(requester)

    commitment_type = _commitment_type_for(payload, work_type)
    commitments: list[CommitmentDraft] = []
    if commitment_type is not None:
        commitments.append(
            _build_commitment(
                record=record,
                payload=payload,
                work_type=work_type,
                commitment_type=commitment_type,
                due_at=due_at,
                owner_id=owner_id,
            )
        )

    return WorkDerivation(
        work_key=work_key_for(
            work_type=work_type,
            source_system=record.source_system,
            source_record_key=record.source_record_key,
        ),
        work_type=work_type,
        title=_title_for(
            payload, record_type=record_type, source_record_key=record.source_record_key
        ),
        domain=domain,
        status=status,
        origin=origin,
        confidence=1.0 if origin is WorkOrigin.EXPLICIT else 0.6,
        priority=priority,
        priority_source=priority_source,
        source_status=_source_status(payload),
        department_id=department_id,
        owner_id=owner_id,
        assigned_user_id=assigned_user_id,
        description=_description_for(payload),
        due_at=due_at,
        due_source=due_source,
        start_at=record.effective_event_time(),
        customer_ref=_party_ref(payload, ("customer", "customer_id", "customer_ref")),
        supplier_ref=_party_ref(payload, ("supplier", "supplier_id", "supplier_ref")),
        order_ref=_party_ref(payload, ("order_id", "order_ref", "po_number", "purchase_order_id")),
        project_ref=_party_ref(payload, ("project_id", "project_ref")),
        machine_ref=_party_ref(payload, ("machine_id", "asset_id", "machine_ref", "equipment_id")),
        production_ref=_party_ref(payload, ("production_id", "work_order_id", "production_ref")),
        attributes=_bounded_attributes(payload),
        assignments=assignments,
        commitments=commitments,
        dependencies=_dependency_drafts(payload),
        mentioned=ownership.mentioned_identities(payload),
        derivations=[DERIVATION_TASK] + ([DERIVATION_COMMITMENT] if commitments else []),
    )


def _source_status(payload: dict[str, Any]) -> str | None:
    value = _first_present(payload, ("status", "state", "workflow_status", "record_status"))
    if value is None:
        return None
    return str(value)[:120]


def _bounded_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep a small, bounded set of non-canonical fields for reference.

    These are copied as-is (never interpreted) so the work detail can show the source context
    without duplicating the whole payload. The set is bounded so this cannot become a second
    copy of the source data.
    """
    keep = (
        "record_kind",
        "identifier",
        "party",
        "currency",
        "amount",
        "quantity",
        "unit",
        "tags",
        "labels",
    )
    bounded: dict[str, Any] = {}
    for key in keep:
        if key in payload and payload[key] not in (None, "", [], {}):
            value = payload[key]
            bounded[key] = value if isinstance(value, (int, float, bool)) else str(value)[:500]
    return bounded


def _build_commitment(
    *,
    record: SourceRecordView,
    payload: dict[str, Any],
    work_type: WorkType,
    commitment_type: CommitmentType,
    due_at: datetime | None,
    owner_id: uuid.UUID | None,
) -> CommitmentDraft:
    committing = _first_present(payload, ("committing_party", "promisor", "supplier", "owner"))
    receiving = _first_present(payload, ("receiving_party", "promisee", "customer", "requester"))
    description = _description_for(payload) or _title_for(
        payload, record_type=record.record_type, source_record_key=record.source_record_key
    )
    return CommitmentDraft(
        commitment_key=f"{commitment_type.value}:{record.source_system}:{record.source_record_key}"[
            :300
        ],
        commitment_type=commitment_type,
        description=description,
        committing_party=str(committing or "unknown")[:200],
        receiving_party=str(receiving or "unknown")[:200],
        commitment_status=_commitment_status_for(payload),
        verification_status=(
            VerificationStatus.SOURCE_REPORTED
            if due_at is not None
            else VerificationStatus.UNRESOLVED
        ),
        confidence=0.8 if due_at is not None else 0.4,
        due_at=due_at,
        related_customer_ref=_party_ref(payload, ("customer", "customer_id", "customer_ref")),
        related_supplier_ref=_party_ref(payload, ("supplier", "supplier_id", "supplier_ref")),
        related_order_ref=_party_ref(payload, ("order_id", "order_ref", "po_number")),
        source_reference=record.source_record_key[:300],
    )


def derivation_version() -> str:
    return WORK_PROCESSING_VERSION


def schema_version() -> str:
    return WORK_SCHEMA_VERSION


__all__ = [
    "DERIVATION_COMMITMENT",
    "DERIVATION_TASK",
    "CommitmentDraft",
    "DependencyDraft",
    "SourceRecordView",
    "WorkDerivation",
    "derive_work",
    "resolve_due_date",
    "work_key_for",
]
