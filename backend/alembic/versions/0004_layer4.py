"""Layer 4 schema: work intelligence engine.

Revision ID: 0004_layer4
Revises: 0003_layer3
Create Date: 2026-01-04

Adds the Layer 4 tables, then applies the same Row-Level Security treatment Layers 1-3 use:

* ``ENABLE`` **and** ``FORCE`` Row-Level Security on every tenant-owned table, with a policy
  requiring ``organization_id = app_current_org()``.
* An ``UPDATE``/``DELETE``-rejecting trigger on ``work_timeline_events``,
  ``work_status_history``, and ``work_commitment_history`` (append-only work history), and on
  ``workflow_definitions`` (immutable workflow versions).

Indexes follow query patterns: list by tenant, filter by tenant+status, tenant+owner,
tenant+department, tenant+due date, and order by tenant+time. JSONB is used only where the
shape is genuinely variable (workflow steps, conflict candidates, extraction fields).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from humisense.db.rls import apply_layer4_rls, remove_layer4_rls

revision: str = "0004_layer4"
down_revision: Union[str, None] = "0003_layer3"
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


def _created() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _updated() -> sa.Column[object]:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


# Enums shared by more than one table are created once and referenced with create_type=False.
_SHARED_ENUMS: dict[str, tuple[str, ...]] = {
    "work_domain": (
        "production",
        "procurement",
        "sales",
        "maintenance",
        "quality",
        "finance",
        "management",
        "general",
    ),
    "work_origin": ("explicit", "extracted", "inferred", "manual"),
    "priority_level": ("low", "normal", "high", "urgent", "critical"),
    "priority_source": ("source", "user", "rule"),
    "deadline_type": (
        "absolute",
        "relative",
        "source_provided",
        "contractual",
        "estimated",
        "requested",
        "recurring",
    ),
    "verification_status": ("verified", "source_reported", "inferred", "unresolved"),
}

_LOCAL_ENUMS: dict[str, tuple[str, ...]] = {
    "work_type": (
        "task",
        "follow_up",
        "approval",
        "purchase_requirement",
        "production_requirement",
        "maintenance_activity",
        "customer_commitment",
        "supplier_commitment",
        "delivery_obligation",
        "document_request",
        "quality_issue",
        "financial_review",
        "operational_issue",
        "project_activity",
        "general_work_item",
    ),
    "work_status": (
        "new",
        "open",
        "assigned",
        "in_progress",
        "waiting",
        "blocked",
        "on_hold",
        "under_review",
        "completed",
        "cancelled",
        "unknown",
    ),
    "assignment_role": (
        "owner",
        "assignee",
        "requester",
        "approver",
        "collaborator",
        "accountable_manager",
        "escalation",
    ),
    "participant_type": ("user", "team", "department", "external"),
    "source_reference_relationship": ("primary", "supporting", "mentioned"),
    "commitment_type": (
        "internal",
        "customer",
        "supplier",
        "departmental",
        "delivery",
        "financial",
        "operational",
    ),
    "commitment_status": (
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
    ),
    "dependency_relationship": (
        "blocks",
        "blocked_by",
        "requires",
        "waiting_for",
        "precedes",
        "related_to",
        "approval_required",
        "material_dependency",
        "information_required",
    ),
    "dependency_target_type": (
        "work",
        "department",
        "supplier",
        "customer",
        "material",
        "approval",
        "machine",
        "document",
        "external_milestone",
    ),
    "dependency_status": ("open", "satisfied", "unknown"),
    "blocker_type": (
        "external",
        "internal",
        "dependency",
        "information",
        "approval",
        "material",
        "capacity",
        "unknown",
    ),
    "blocker_status": ("open", "resolved", "unknown"),
    "followup_target_type": ("person", "department", "supplier", "customer", "internal"),
    "followup_status": ("suggested", "open", "overdue", "done", "cancelled"),
    "urgency": ("low", "normal", "high", "urgent"),
    "workflow_instance_status": ("active", "completed", "cancelled", "unknown"),
    "workflow_step_state": (
        "pending",
        "active",
        "waiting",
        "blocked",
        "completed",
        "skipped",
        "unknown",
    ),
    "work_conflict_type": (
        "duplicate_work",
        "conflicting_owner",
        "conflicting_deadline",
        "inconsistent_status",
        "missing_reference",
        "unresolved_identity",
        "ambiguous_organization",
        "inconsistent_party_id",
        "contradictory_commitment",
        "stale_source",
        "source_sync_conflict",
        "circular_dependency",
    ),
    "work_conflict_status": ("open", "resolved", "accepted"),
    "extraction_candidate_type": (
        "task",
        "commitment",
        "deadline",
        "dependency",
        "owner",
        "blocker",
    ),
    "extraction_verification": ("unverified", "verified", "rejected"),
    "work_event_status": ("pending", "published", "failed"),
}

_ALL_ENUMS: dict[str, tuple[str, ...]] = {**_SHARED_ENUMS, **_LOCAL_ENUMS}

_LAYER4_TABLES_IN_DROP_ORDER = (
    "work_outbox_events",
    "work_extractions",
    "work_conflicts",
    "work_timeline_events",
    "workflow_steps",
    "workflow_instances",
    "workflow_definitions",
    "work_followups",
    "work_blockers",
    "work_dependencies",
    "work_commitment_history",
    "work_commitments",
    "work_priorities",
    "work_status_history",
    "work_assignments",
    "work_source_references",
    "work_entities",
    "departments",
)


def upgrade() -> None:
    for name, values in _ALL_ENUMS.items():
        postgresql.ENUM(*values, name=name, create_type=False).create(op.get_bind(), checkfirst=True)

    def enum(name: str) -> postgresql.ENUM:
        return postgresql.ENUM(*_ALL_ENUMS[name], name=name, create_type=False)

    # --- Departments ---------------------------------------------------------
    op.create_table(
        "departments",
        _uuid(),
        _org(),
        sa.Column("department_key", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("domain", enum("work_domain"), nullable=False, server_default="general"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _created(),
        _updated(),
        sa.UniqueConstraint("organization_id", "department_key", name="uq_departments_org_key"),
    )
    op.create_index("ix_departments_organization_id", "departments", ["organization_id"])

    # --- Work entities -------------------------------------------------------
    op.create_table(
        "work_entities",
        _uuid(),
        _org(),
        sa.Column("work_key", sa.String(300), nullable=False),
        sa.Column("work_type", enum("work_type"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("domain", enum("work_domain"), nullable=False, server_default="general"),
        sa.Column(
            "department_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
        ),
        sa.Column("owner_id", sa.Uuid(as_uuid=True)),
        sa.Column("assigned_user_id", sa.Uuid(as_uuid=True)),
        sa.Column("creator_id", sa.Uuid(as_uuid=True)),
        sa.Column("priority", enum("priority_level"), nullable=False, server_default="normal"),
        sa.Column("priority_source", enum("priority_source"), nullable=False, server_default="source"),
        sa.Column("status", enum("work_status"), nullable=False, server_default="new"),
        sa.Column("source_status", sa.String(120)),
        sa.Column("origin", enum("work_origin"), nullable=False, server_default="inferred"),
        sa.Column("confidence", sa.Float()),
        sa.Column("start_at", sa.DateTime(timezone=True)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("due_source", enum("deadline_type")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "parent_work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="SET NULL"),
        ),
        sa.Column("customer_ref", sa.String(200)),
        sa.Column("supplier_ref", sa.String(200)),
        sa.Column("project_ref", sa.String(200)),
        sa.Column("order_ref", sa.String(200)),
        sa.Column("machine_ref", sa.String(200)),
        sa.Column("production_ref", sa.String(200)),
        sa.Column("attributes", JSONB, nullable=False, server_default="{}"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        _created(),
        _updated(),
        sa.UniqueConstraint("organization_id", "work_key", name="uq_work_entities_org_key"),
    )
    op.create_index("ix_work_entities_organization_id", "work_entities", ["organization_id"])
    op.create_index("ix_work_entities_org_status", "work_entities", ["organization_id", "status"])
    op.create_index("ix_work_entities_org_type", "work_entities", ["organization_id", "work_type"])
    op.create_index("ix_work_entities_org_domain", "work_entities", ["organization_id", "domain"])
    op.create_index(
        "ix_work_entities_org_department", "work_entities", ["organization_id", "department_id"]
    )
    op.create_index("ix_work_entities_org_owner", "work_entities", ["organization_id", "owner_id"])
    op.create_index("ix_work_entities_org_due", "work_entities", ["organization_id", "due_at"])
    op.create_index("ix_work_entities_org_updated", "work_entities", ["organization_id", "updated_at"])
    op.create_index("ix_work_entities_parent", "work_entities", ["parent_work_id"])

    # --- Source references ---------------------------------------------------
    op.create_table(
        "work_source_references",
        _uuid(),
        _org(),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "canonical_record_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("canonical_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "raw_record_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("raw_records.id", ondelete="SET NULL"),
        ),
        sa.Column("source_system", sa.String(120), nullable=False),
        sa.Column("source_record_key", sa.String(512), nullable=False),
        sa.Column("source_record_type", sa.String(120), nullable=False),
        sa.Column(
            "relationship",
            enum("source_reference_relationship"),
            nullable=False,
            server_default="supporting",
        ),
        sa.Column("confidence", sa.Float()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        sa.UniqueConstraint(
            "organization_id",
            "work_id",
            "canonical_record_id",
            name="uq_work_source_references_org_work_record",
        ),
    )
    op.create_index(
        "ix_work_source_references_organization_id", "work_source_references", ["organization_id"]
    )
    op.create_index("ix_work_source_references_work", "work_source_references", ["work_id"])
    op.create_index(
        "ix_work_source_references_canonical", "work_source_references", ["canonical_record_id"]
    )

    # --- Assignments ---------------------------------------------------------
    op.create_table(
        "work_assignments",
        _uuid(),
        _org(),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("participant_type", enum("participant_type"), nullable=False, server_default="user"),
        sa.Column("user_id", sa.Uuid(as_uuid=True)),
        sa.Column("department_id", sa.Uuid(as_uuid=True)),
        sa.Column("external_name", sa.String(200)),
        sa.Column("role", enum("assignment_role"), nullable=False),
        sa.Column(
            "verification", enum("verification_status"), nullable=False, server_default="unresolved"
        ),
        sa.Column("confidence", sa.Float()),
        sa.Column(
            "source_reference_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_source_references.id", ondelete="SET NULL"),
        ),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        _updated(),
    )
    op.create_index("ix_work_assignments_organization_id", "work_assignments", ["organization_id"])
    op.create_index("ix_work_assignments_work", "work_assignments", ["work_id"])
    op.create_index("ix_work_assignments_org_user", "work_assignments", ["organization_id", "user_id"])
    op.create_index(
        "ix_work_assignments_org_department", "work_assignments", ["organization_id", "department_id"]
    )

    # --- Status history ------------------------------------------------------
    op.create_table(
        "work_status_history",
        _uuid(),
        _org(),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(30)),
        sa.Column("to_status", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True)),
        sa.Column("reason", sa.Text()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
    )
    op.create_index("ix_work_status_history_organization_id", "work_status_history", ["organization_id"])
    op.create_index(
        "ix_work_status_history_work", "work_status_history", ["work_id", "created_at"]
    )

    # --- Priorities ----------------------------------------------------------
    op.create_table(
        "work_priorities",
        _uuid(),
        _org(),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("level", enum("priority_level"), nullable=False, server_default="normal"),
        sa.Column("source", enum("priority_source"), nullable=False),
        sa.Column("overdue", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("due_soon", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dependency_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("waiting_duration_seconds", sa.Integer()),
        sa.Column("commitment_status", sa.String(30)),
        sa.Column("source_completeness", sa.Float(), nullable=False, server_default="0"),
        sa.Column("related_detection_ids", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "organization_id", "work_id", "source", name="uq_work_priorities_org_work_source"
        ),
    )
    op.create_index("ix_work_priorities_organization_id", "work_priorities", ["organization_id"])
    op.create_index("ix_work_priorities_work", "work_priorities", ["work_id"])
    op.create_index("ix_work_priorities_org_level", "work_priorities", ["organization_id", "level"])

    # --- Commitments ---------------------------------------------------------
    op.create_table(
        "work_commitments",
        _uuid(),
        _org(),
        sa.Column("commitment_key", sa.String(300), nullable=False),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="SET NULL"),
        ),
        sa.Column("commitment_type", enum("commitment_type"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("committing_party", sa.String(200), nullable=False),
        sa.Column("receiving_party", sa.String(200), nullable=False),
        sa.Column("responsible_owner", sa.Uuid(as_uuid=True)),
        sa.Column("related_customer_ref", sa.String(200)),
        sa.Column("related_supplier_ref", sa.String(200)),
        sa.Column("related_order_ref", sa.String(200)),
        sa.Column("promised_at", sa.DateTime(timezone=True)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("actual_completion_at", sa.DateTime(timezone=True)),
        sa.Column(
            "commitment_status", enum("commitment_status"), nullable=False, server_default="proposed"
        ),
        sa.Column(
            "verification_status",
            enum("verification_status"),
            nullable=False,
            server_default="unresolved",
        ),
        sa.Column("confidence", sa.Float()),
        sa.Column("source_reference", sa.String(300)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        _updated(),
        sa.UniqueConstraint("organization_id", "commitment_key", name="uq_work_commitments_org_key"),
    )
    op.create_index("ix_work_commitments_organization_id", "work_commitments", ["organization_id"])
    op.create_index("ix_work_commitments_work", "work_commitments", ["work_id"])
    op.create_index("ix_work_commitments_org_due", "work_commitments", ["organization_id", "due_at"])
    op.create_index(
        "ix_work_commitments_org_status", "work_commitments", ["organization_id", "commitment_status"]
    )

    op.create_table(
        "work_commitment_history",
        _uuid(),
        _org(),
        sa.Column(
            "commitment_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_commitments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(30)),
        sa.Column("to_status", sa.String(30), nullable=False),
        sa.Column("previous_due_at", sa.DateTime(timezone=True)),
        sa.Column("new_due_at", sa.DateTime(timezone=True)),
        sa.Column("actor_id", sa.Uuid(as_uuid=True)),
        sa.Column("reason", sa.Text()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
    )
    op.create_index(
        "ix_work_commitment_history_organization_id", "work_commitment_history", ["organization_id"]
    )
    op.create_index(
        "ix_work_commitment_history_commitment",
        "work_commitment_history",
        ["commitment_id", "created_at"],
    )

    # --- Dependencies --------------------------------------------------------
    op.create_table(
        "work_dependencies",
        _uuid(),
        _org(),
        sa.Column(
            "source_work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "target_entity_type", enum("dependency_target_type"), nullable=False, server_default="work"
        ),
        sa.Column("target_entity_ref", sa.String(300)),
        sa.Column("relationship", enum("dependency_relationship"), nullable=False),
        sa.Column("status", enum("dependency_status"), nullable=False, server_default="unknown"),
        sa.Column("origin", enum("work_origin"), nullable=False, server_default="inferred"),
        sa.Column("confidence", sa.Float()),
        sa.Column(
            "verification_status",
            enum("verification_status"),
            nullable=False,
            server_default="unresolved",
        ),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        _updated(),
    )
    op.create_index("ix_work_dependencies_organization_id", "work_dependencies", ["organization_id"])
    op.create_index("ix_work_dependencies_source", "work_dependencies", ["source_work_id"])
    op.create_index("ix_work_dependencies_target", "work_dependencies", ["target_work_id"])

    # --- Blockers ------------------------------------------------------------
    op.create_table(
        "work_blockers",
        _uuid(),
        _org(),
        sa.Column(
            "affected_work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("blocker_type", enum("blocker_type"), nullable=False, server_default="unknown"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("blocking_entity", sa.String(300)),
        sa.Column(
            "responsible_department_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_resolution_at", sa.DateTime(timezone=True)),
        sa.Column("blocker_status", enum("blocker_status"), nullable=False, server_default="open"),
        sa.Column("evidence_references", JSONB, nullable=False, server_default="[]"),
        sa.Column("provenance", enum("work_origin"), nullable=False, server_default="inferred"),
        sa.Column(
            "verification_status",
            enum("verification_status"),
            nullable=False,
            server_default="unresolved",
        ),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        _updated(),
    )
    op.create_index("ix_work_blockers_organization_id", "work_blockers", ["organization_id"])
    op.create_index("ix_work_blockers_work", "work_blockers", ["affected_work_id"])
    op.create_index("ix_work_blockers_org_status", "work_blockers", ["organization_id", "blocker_status"])

    # --- Follow-ups ----------------------------------------------------------
    op.create_table(
        "work_followups",
        _uuid(),
        _org(),
        sa.Column(
            "related_work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_type", enum("followup_target_type"), nullable=False),
        sa.Column("target_ref", sa.String(300), nullable=False),
        sa.Column("owner_id", sa.Uuid(as_uuid=True)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("next_due_at", sa.DateTime(timezone=True)),
        sa.Column("urgency", enum("urgency"), nullable=False, server_default="normal"),
        sa.Column("follow_up_status", enum("followup_status"), nullable=False, server_default="suggested"),
        sa.Column("rule_id", sa.String(120), nullable=False),
        sa.Column("provenance", sa.String(20), nullable=False, server_default="rule"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        _updated(),
    )
    op.create_index("ix_work_followups_organization_id", "work_followups", ["organization_id"])
    op.create_index("ix_work_followups_work", "work_followups", ["related_work_id"])
    op.create_index(
        "ix_work_followups_org_status", "work_followups", ["organization_id", "follow_up_status"]
    )
    op.create_index("ix_work_followups_org_due", "work_followups", ["organization_id", "next_due_at"])

    # --- Workflows -----------------------------------------------------------
    op.create_table(
        "workflow_definitions",
        _uuid(),
        _org(),
        sa.Column("workflow_key", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("workflow_type", sa.String(80), nullable=False),
        sa.Column(
            "department_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
        ),
        sa.Column("entry_conditions", JSONB, nullable=False, server_default="{}"),
        sa.Column("exit_conditions", JSONB, nullable=False, server_default="{}"),
        sa.Column("steps", JSONB, nullable=False, server_default="[]"),
        sa.Column("status_mappings", JSONB, nullable=False, server_default="{}"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        sa.UniqueConstraint(
            "organization_id",
            "workflow_key",
            "version",
            name="uq_workflow_definitions_org_key_version",
        ),
    )
    op.create_index(
        "ix_workflow_definitions_organization_id", "workflow_definitions", ["organization_id"]
    )
    op.create_index(
        "ix_workflow_definitions_org_type", "workflow_definitions", ["organization_id", "workflow_type"]
    )

    op.create_table(
        "workflow_instances",
        _uuid(),
        _org(),
        sa.Column(
            "definition_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("workflow_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", enum("workflow_instance_status"), nullable=False, server_default="active"),
        sa.Column("current_step_key", sa.String(120)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        _updated(),
    )
    op.create_index("ix_workflow_instances_organization_id", "workflow_instances", ["organization_id"])
    op.create_index("ix_workflow_instances_definition", "workflow_instances", ["definition_id"])
    op.create_index("ix_workflow_instances_work", "workflow_instances", ["work_id"])

    op.create_table(
        "workflow_steps",
        _uuid(),
        _org(),
        sa.Column(
            "instance_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("workflow_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_key", sa.String(120), nullable=False),
        sa.Column("step_name", sa.String(200), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("responsible_role", sa.String(80)),
        sa.Column("observed_state", enum("workflow_step_state"), nullable=False, server_default="unknown"),
        sa.Column("depends_on", JSONB, nullable=False, server_default="[]"),
        sa.Column("source_reference", JSONB),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        _updated(),
        sa.UniqueConstraint(
            "organization_id", "instance_id", "step_key", name="uq_workflow_steps_instance_step"
        ),
    )
    op.create_index("ix_workflow_steps_organization_id", "workflow_steps", ["organization_id"])
    op.create_index("ix_workflow_steps_instance", "workflow_steps", ["instance_id"])

    # --- Timeline ------------------------------------------------------------
    op.create_table(
        "work_timeline_events",
        _uuid(),
        _org(),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True)),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True)),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="system"),
        sa.Column("previous_state", JSONB),
        sa.Column("new_state", JSONB),
        sa.Column("source_reference", JSONB),
        sa.Column("provenance", sa.String(20), nullable=False, server_default="inferred"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
    )
    op.create_index(
        "ix_work_timeline_events_organization_id", "work_timeline_events", ["organization_id"]
    )
    op.create_index(
        "ix_work_timeline_events_work", "work_timeline_events", ["work_id", "event_at"]
    )
    op.create_index(
        "ix_work_timeline_events_org_type", "work_timeline_events", ["organization_id", "event_type"]
    )

    # --- Conflicts -----------------------------------------------------------
    op.create_table(
        "work_conflicts",
        _uuid(),
        _org(),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="SET NULL"),
        ),
        sa.Column("entity_type", sa.String(60), nullable=False),
        sa.Column("entity_key", sa.String(300), nullable=False),
        sa.Column("conflict_type", enum("work_conflict_type"), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("candidates", JSONB, nullable=False, server_default="[]"),
        sa.Column("status", enum("work_conflict_status"), nullable=False, server_default="open"),
        sa.Column("resolved_value", JSONB),
        sa.Column("resolved_by", sa.Uuid(as_uuid=True)),
        sa.Column("resolution_note", sa.Text()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        _created(),
        sa.UniqueConstraint(
            "organization_id",
            "entity_type",
            "entity_key",
            "conflict_type",
            name="uq_work_conflicts_org_entity_type",
        ),
    )
    op.create_index("ix_work_conflicts_organization_id", "work_conflicts", ["organization_id"])
    op.create_index("ix_work_conflicts_org_status", "work_conflicts", ["organization_id", "status"])
    op.create_index("ix_work_conflicts_work", "work_conflicts", ["work_id"])

    # --- Extractions ---------------------------------------------------------
    op.create_table(
        "work_extractions",
        _uuid(),
        _org(),
        sa.Column("extraction_key", sa.String(300), nullable=False),
        sa.Column(
            "canonical_record_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("canonical_records.id", ondelete="SET NULL"),
        ),
        sa.Column("source_reference", sa.String(300)),
        sa.Column("extraction_method", sa.String(120), nullable=False),
        sa.Column("extraction_version", sa.String(40), nullable=False),
        sa.Column("candidate_type", enum("extraction_candidate_type"), nullable=False),
        sa.Column("fields", JSONB, nullable=False, server_default="{}"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "verification_status",
            enum("extraction_verification"),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column(
            "promoted_work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="SET NULL"),
        ),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        sa.UniqueConstraint("organization_id", "extraction_key", name="uq_work_extractions_org_key"),
    )
    op.create_index("ix_work_extractions_organization_id", "work_extractions", ["organization_id"])
    op.create_index(
        "ix_work_extractions_org_status", "work_extractions", ["organization_id", "verification_status"]
    )

    # --- Outbox --------------------------------------------------------------
    op.create_table(
        "work_outbox_events",
        _uuid(),
        _org(),
        sa.Column(
            "work_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("work_entities.id", ondelete="CASCADE"),
        ),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(300), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("status", enum("work_event_status"), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        _created(),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_work_outbox_org_key"),
    )
    op.create_index("ix_work_outbox_events_organization_id", "work_outbox_events", ["organization_id"])
    op.create_index(
        "ix_work_outbox_events_org_status", "work_outbox_events", ["organization_id", "status"]
    )
    op.create_index(
        "ix_work_outbox_events_status_created", "work_outbox_events", ["status", "created_at"]
    )

    apply_layer4_rls()


def downgrade() -> None:
    remove_layer4_rls()
    for table in _LAYER4_TABLES_IN_DROP_ORDER:
        op.drop_table(table)
    for name in reversed(list(_ALL_ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name}")
