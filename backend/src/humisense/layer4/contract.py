"""Layer 4's stable, versioned contracts.

These are the shapes every later layer reads. They are versioned
(``WORK_SCHEMA_VERSION``) so a consumer can pin what it understands and Layer 4 can evolve
without breaking it.

Design rules encoded here, and enforced by the validators:

* **Provenance, not payloads.** A work entity references the Layer 2 canonical records that
  describe it; it never copies a source payload. A blocker's evidence is references, not
  values.
* **Uncertainty is explicit.** ``origin`` and ``confidence`` are first-class. An inferred or
  extracted value is never shaped like a confirmed one, and an unresolved owner or date is
  ``None``, never a fabricated value.
* **The four work times are never conflated.** A timeline event carries when it occurred,
  the source's own timestamp, when Layer 4 ingested it, and when Layer 4 processed it.
* **A requested date is not a commitment.** Deadline and commitment types distinguish a
  request from a confirmed promise.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

WORK_SCHEMA_VERSION = "4.0"
"""Version of the work-domain contract produced by Layer 4."""

WORK_PROCESSING_VERSION = "layer4-0.1.0"
"""Version of the derivation code. Bumped when a change would produce different output."""

MAX_ATTRIBUTE_KEYS = 50
MAX_EVIDENCE_REFS = 50
"""A blocker cites at most this many supporting records."""

Origin = Literal["explicit", "extracted", "inferred", "manual"]
Verification = Literal["verified", "source_reported", "inferred", "unresolved"]


class WorkProvenance(BaseModel):
    """Where a derived work value came from and how it was produced."""

    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(min_length=1, max_length=120)
    canonical_record_id: uuid.UUID | None = None
    source_record_key: str | None = Field(default=None, max_length=512)
    derivation: str = Field(min_length=1, max_length=120)
    """The derivation rule that produced the value, for example ``task_from_record``."""
    derivation_version: str = Field(default=WORK_PROCESSING_VERSION, max_length=40)
    origin: Origin
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    correlation_id: str = Field(min_length=1, max_length=64)


class WorkSourceReference(BaseModel):
    """A link from a work entity to a source record that describes it."""

    model_config = ConfigDict(extra="forbid")

    canonical_record_id: uuid.UUID
    raw_record_id: uuid.UUID | None = None
    source_system: str = Field(min_length=1, max_length=120)
    source_record_key: str = Field(min_length=1, max_length=512)
    source_record_type: str = Field(min_length=1, max_length=120)
    relationship: Literal["primary", "supporting", "mentioned"] = "supporting"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class WorkAssignment(BaseModel):
    """One participant in a work item and the role they hold."""

    model_config = ConfigDict(extra="forbid")

    participant_type: Literal["user", "team", "department", "external"]
    user_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    external_name: str | None = Field(default=None, max_length=200)
    role: Literal[
        "owner",
        "assignee",
        "requester",
        "approver",
        "collaborator",
        "accountable_manager",
        "escalation",
    ]
    verification: Verification = "unresolved"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("external_name")
    @classmethod
    def _external_needs_a_name(cls, value: str | None, info: Any) -> str | None:
        if info.data.get("participant_type") == "external" and not value:
            raise ValueError("an external participant must have a name")
        return value


class WorkPriority(BaseModel):
    """Deterministic prioritization metadata. Never authoritative over a human priority."""

    model_config = ConfigDict(extra="forbid")

    level: Literal["low", "normal", "high", "urgent", "critical"]
    source: Literal["source", "user", "rule"]
    overdue: bool = False
    due_soon: bool = False
    blocked: bool = False
    dependency_count: int = Field(default=0, ge=0)
    waiting_duration_seconds: int | None = Field(default=None, ge=0)
    commitment_status: str | None = Field(default=None, max_length=30)
    source_completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    related_detection_ids: list[uuid.UUID] = Field(default_factory=list)
    computed_at: datetime | None = None


class WorkEntity(BaseModel):
    """The canonical unit of work.

    Field names follow the Layer 4 specification. ``owner_id`` and ``due_at`` are ``None``
    when the source did not state them; that is the unresolved state, not a default.
    """

    model_config = ConfigDict(extra="forbid")

    work_id: uuid.UUID
    tenant_id: uuid.UUID
    work_key: str = Field(min_length=1, max_length=300)
    work_type: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    domain: str = Field(min_length=1, max_length=40)
    department_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    assigned_user_id: uuid.UUID | None = None
    creator_id: uuid.UUID | None = None
    priority: Literal["low", "normal", "high", "urgent", "critical"] = "normal"
    priority_source: Literal["source", "user", "rule"] = "source"
    status: str = Field(min_length=1, max_length=30)
    source_status: str | None = Field(default=None, max_length=120)
    origin: Origin = "inferred"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    start_at: datetime | None = None
    due_at: datetime | None = None
    due_source: str | None = Field(default=None, max_length=30)
    completed_at: datetime | None = None
    parent_work_id: uuid.UUID | None = None
    customer_ref: str | None = Field(default=None, max_length=200)
    supplier_ref: str | None = Field(default=None, max_length=200)
    project_ref: str | None = Field(default=None, max_length=200)
    order_ref: str | None = Field(default=None, max_length=200)
    machine_ref: str | None = Field(default=None, max_length=200)
    production_ref: str | None = Field(default=None, max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_references: list[WorkSourceReference] = Field(default_factory=list)
    correlation_id: str = Field(min_length=1, max_length=64)
    schema_version: str = WORK_SCHEMA_VERSION
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware; naive datetimes are refused")
        return value.astimezone(UTC)

    @field_validator("attributes")
    @classmethod
    def _bound_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_ATTRIBUTE_KEYS:
            raise ValueError(f"attributes must not exceed {MAX_ATTRIBUTE_KEYS} keys")
        return value


class WorkCommitment(BaseModel):
    """A promise made by one party to another, with its verification state."""

    model_config = ConfigDict(extra="forbid")

    commitment_id: uuid.UUID
    tenant_id: uuid.UUID
    commitment_type: Literal[
        "internal", "customer", "supplier", "departmental", "delivery", "financial", "operational"
    ]
    description: str = Field(min_length=1)
    committing_party: str = Field(min_length=1, max_length=200)
    receiving_party: str = Field(min_length=1, max_length=200)
    responsible_owner: uuid.UUID | None = None
    work_id: uuid.UUID | None = None
    related_customer_ref: str | None = Field(default=None, max_length=200)
    related_supplier_ref: str | None = Field(default=None, max_length=200)
    related_order_ref: str | None = Field(default=None, max_length=200)
    promised_at: datetime | None = None
    due_at: datetime | None = None
    actual_completion_at: datetime | None = None
    commitment_status: Literal[
        "proposed",
        "confirmed",
        "requested",
        "estimated",
        "contractual",
        "inferred",
        "at_risk",
        "met",
        "missed",
        "cancelled",
    ] = "proposed"
    verification_status: Verification = "unresolved"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_reference: str | None = Field(default=None, max_length=300)
    correlation_id: str = Field(min_length=1, max_length=64)
    created_at: datetime
    updated_at: datetime


class WorkDependency(BaseModel):
    """A typed edge from a work item to another work item or an external entity."""

    model_config = ConfigDict(extra="forbid")

    dependency_id: uuid.UUID
    tenant_id: uuid.UUID
    source_work_id: uuid.UUID
    target_work_id: uuid.UUID | None = None
    target_entity_type: Literal[
        "work",
        "department",
        "supplier",
        "customer",
        "material",
        "approval",
        "machine",
        "document",
        "external_milestone",
    ] = "work"
    target_entity_ref: str | None = Field(default=None, max_length=300)
    relationship: Literal[
        "blocks",
        "blocked_by",
        "requires",
        "waiting_for",
        "precedes",
        "related_to",
        "approval_required",
        "material_dependency",
        "information_required",
    ]
    status: Literal["open", "satisfied", "unknown"] = "unknown"
    origin: Origin = "inferred"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    verification_status: Verification = "unresolved"
    correlation_id: str = Field(min_length=1, max_length=64)
    created_at: datetime
    updated_at: datetime


class WorkBlocker(BaseModel):
    """Why work is not progressing.

    ``blocker_type = "unknown"`` and ``verification_status = "unresolved"`` are the honest
    representation when the reason is not evidenced.
    """

    model_config = ConfigDict(extra="forbid")

    blocker_id: uuid.UUID
    tenant_id: uuid.UUID
    affected_work_id: uuid.UUID
    blocker_type: Literal[
        "external",
        "internal",
        "dependency",
        "information",
        "approval",
        "material",
        "capacity",
        "unknown",
    ] = "unknown"
    description: str = Field(min_length=1)
    blocking_entity: str | None = Field(default=None, max_length=300)
    responsible_department_id: uuid.UUID | None = None
    detected_at: datetime
    expected_resolution_at: datetime | None = None
    blocker_status: Literal["open", "resolved", "unknown"] = "open"
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)
    provenance: Origin = "inferred"
    verification_status: Verification = "unresolved"
    correlation_id: str = Field(min_length=1, max_length=64)

    @field_validator("evidence_references")
    @classmethod
    def _bound_evidence(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(value) > MAX_EVIDENCE_REFS:
            raise ValueError(f"evidence_references must not exceed {MAX_EVIDENCE_REFS}")
        return value


class WorkFollowUp(BaseModel):
    """Something requiring follow-up, produced by a documented deterministic rule."""

    model_config = ConfigDict(extra="forbid")

    follow_up_id: uuid.UUID
    tenant_id: uuid.UUID
    related_work_id: uuid.UUID
    target_type: Literal["person", "department", "supplier", "customer", "internal"]
    target_ref: str = Field(min_length=1, max_length=300)
    owner_id: uuid.UUID | None = None
    reason: str = Field(min_length=1)
    last_activity_at: datetime | None = None
    next_due_at: datetime | None = None
    urgency: Literal["low", "normal", "high", "urgent"] = "normal"
    follow_up_status: Literal["suggested", "open", "overdue", "done", "cancelled"] = "suggested"
    rule_id: str = Field(min_length=1, max_length=120)
    provenance: Literal["rule", "explicit"] = "rule"
    correlation_id: str = Field(min_length=1, max_length=64)


class WorkTimelineEvent(BaseModel):
    """One append-only activity record, with the four work times preserved separately."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    tenant_id: uuid.UUID
    work_id: uuid.UUID
    event_type: str = Field(min_length=1, max_length=60)
    event_at: datetime
    source_timestamp: datetime | None = None
    ingested_at: datetime
    processed_at: datetime
    actor_id: uuid.UUID | None = None
    actor_type: Literal["user", "system", "worker", "source"] = "system"
    previous_state: dict[str, Any] | None = None
    new_state: dict[str, Any] | None = None
    source_reference: dict[str, Any] | None = None
    provenance: Origin = "inferred"
    correlation_id: str = Field(min_length=1, max_length=64)


class WorkConflict(BaseModel):
    """A detected conflict. Never silently resolved."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: uuid.UUID
    tenant_id: uuid.UUID
    work_id: uuid.UUID | None = None
    entity_type: str = Field(min_length=1, max_length=60)
    entity_key: str = Field(min_length=1, max_length=300)
    conflict_type: str = Field(min_length=1, max_length=40)
    detail: str = Field(min_length=1)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["open", "resolved", "accepted"] = "open"
    detected_at: datetime


class WorkExtraction(BaseModel):
    """A candidate work fact received from an approved upstream extractor.

    An extraction is a candidate, never an assignment. It becomes work only through a
    deterministic promotion path or an authorized action.
    """

    model_config = ConfigDict(extra="forbid")

    extraction_id: uuid.UUID
    tenant_id: uuid.UUID
    extraction_key: str = Field(min_length=1, max_length=300)
    canonical_record_id: uuid.UUID | None = None
    source_reference: str | None = Field(default=None, max_length=300)
    extraction_method: str = Field(min_length=1, max_length=120)
    extraction_version: str = Field(min_length=1, max_length=40)
    candidate_type: Literal["task", "commitment", "deadline", "dependency", "owner", "blocker"]
    fields: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verification_status: Literal["unverified", "verified", "rejected"] = "unverified"
    promoted_work_id: uuid.UUID | None = None
    correlation_id: str = Field(min_length=1, max_length=64)
    created_at: datetime


class WorkEvent(BaseModel):
    """A Layer 4 output event, carried on the work outbox."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    event_type: str = Field(min_length=1, max_length=80)
    tenant_id: uuid.UUID
    schema_version: str = WORK_SCHEMA_VERSION
    entity_type: str = Field(min_length=1, max_length=40)
    entity_id: uuid.UUID
    work_id: uuid.UUID | None = None
    source_reference: str | None = Field(default=None, max_length=300)
    occurred_at: datetime
    correlation_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=300)
    provenance: Origin = "inferred"
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowStepDefinition(BaseModel):
    """One step of a configurable workflow definition."""

    model_config = ConfigDict(extra="forbid")

    step_key: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    responsible_role: str | None = Field(default=None, max_length=80)
    depends_on: list[str] = Field(default_factory=list)
    expected_duration_seconds: int | None = Field(default=None, ge=0)
    source_status_hint: str | None = Field(default=None, max_length=120)


class WorkflowDefinition(BaseModel):
    """A versioned, tenant-configurable workflow."""

    model_config = ConfigDict(extra="forbid")

    definition_id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_key: str = Field(min_length=1, max_length=120)
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=200)
    workflow_type: str = Field(min_length=1, max_length=80)
    department_id: uuid.UUID | None = None
    entry_conditions: dict[str, Any] = Field(default_factory=dict)
    exit_conditions: dict[str, Any] = Field(default_factory=dict)
    steps: list[WorkflowStepDefinition] = Field(default_factory=list)
    status_mappings: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "MAX_ATTRIBUTE_KEYS",
    "MAX_EVIDENCE_REFS",
    "WORK_PROCESSING_VERSION",
    "WORK_SCHEMA_VERSION",
    "WorkAssignment",
    "WorkBlocker",
    "WorkCommitment",
    "WorkConflict",
    "WorkDependency",
    "WorkEntity",
    "WorkEvent",
    "WorkExtraction",
    "WorkFollowUp",
    "WorkPriority",
    "WorkProvenance",
    "WorkSourceReference",
    "WorkTimelineEvent",
    "WorkflowDefinition",
    "WorkflowStepDefinition",
]
