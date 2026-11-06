"""Workflow intelligence.

A workflow is a tenant-configured, versioned sequence of steps. Two rules keep workflow state
honest:

* **Observed state is separate from expected state.** The observed state of a step is derived
  from verified source statuses and dependency states. The expected state is the definition.
  They are reported side by side and never conflated.
* **A step is never advanced by elapsed time alone.** ``expected_duration_seconds`` is
  informational; it never moves a step.

The reference workflows in the brief (purchase-to-production, RFQ-to-order, maintenance) are
shipped as optional **seed definitions** in :data:`REFERENCE_WORKFLOWS`. They are data, not
engine logic: a tenant may replace them, and the engine has no manufacturing knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from humisense.db.models import WorkflowStepState


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """One step of a workflow definition."""

    step_key: str
    name: str
    responsible_role: str | None = None
    depends_on: tuple[str, ...] = ()
    expected_duration_seconds: int | None = None
    source_status_hint: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowTemplate:
    """A workflow definition as data, ready to seed for a tenant."""

    workflow_key: str
    name: str
    workflow_type: str
    steps: tuple[StepDefinition, ...]
    entry_conditions: dict[str, object] = field(default_factory=dict)
    exit_conditions: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepObservation:
    """The observed state of one step, derived from source facts."""

    step_key: str
    state: WorkflowStepState
    source_reference: dict[str, object] | None = None


def compute_observed_steps(
    *,
    definition_steps: tuple[StepDefinition, ...],
    completed_step_keys: frozenset[str],
    active_step_keys: frozenset[str],
    blocked_step_keys: frozenset[str],
) -> list[StepObservation]:
    """Derive the observed state of every step from verified facts.

    A step is ``COMPLETED`` only when a source fact says so, ``BLOCKED`` only when a
    dependency says so, and ``PENDING`` otherwise. Nothing is inferred from time.
    """
    observations: list[StepObservation] = []
    for step in definition_steps:
        if step.step_key in completed_step_keys:
            state = WorkflowStepState.COMPLETED
        elif step.step_key in blocked_step_keys:
            state = WorkflowStepState.BLOCKED
        elif step.step_key in active_step_keys:
            state = WorkflowStepState.ACTIVE
        else:
            # A step whose dependencies are not yet complete is WAITING, not PENDING. This
            # is still derived from facts (the dependency graph), not from elapsed time.
            unmet = [dep for dep in step.depends_on if dep not in completed_step_keys]
            state = WorkflowStepState.WAITING if unmet else WorkflowStepState.PENDING
        observations.append(StepObservation(step_key=step.step_key, state=state))
    return observations


def current_step(
    definition_steps: tuple[StepDefinition, ...], observations: list[StepObservation]
) -> str | None:
    """Return the first non-completed step key in definition order, or None when finished."""
    state_by_key = {obs.step_key: obs.state for obs in observations}
    for step in definition_steps:
        if state_by_key.get(step.step_key) not in {
            WorkflowStepState.COMPLETED,
            WorkflowStepState.SKIPPED,
        }:
            return step.step_key
    return None


# --- Reference workflows (seed data, not engine logic) ---------------------------

PURCHASE_TO_PRODUCTION = WorkflowTemplate(
    workflow_key="purchase_to_production",
    name="Purchase to Production",
    workflow_type="procurement",
    steps=(
        StepDefinition("material_requirement", "Material requirement"),
        StepDefinition("rfq", "RFQ", depends_on=("material_requirement",)),
        StepDefinition("quotation", "Supplier quotation", depends_on=("rfq",)),
        StepDefinition("approval", "Approval", depends_on=("quotation",)),
        StepDefinition("purchase_order", "Purchase order", depends_on=("approval",)),
        StepDefinition(
            "supplier_commitment", "Supplier commitment", depends_on=("purchase_order",)
        ),
        StepDefinition("delivery", "Material delivery", depends_on=("supplier_commitment",)),
        StepDefinition("incoming_inspection", "Incoming inspection", depends_on=("delivery",)),
        StepDefinition(
            "material_availability", "Material availability", depends_on=("incoming_inspection",)
        ),
        StepDefinition(
            "production_readiness", "Production readiness", depends_on=("material_availability",)
        ),
    ),
)

RFQ_TO_ORDER = WorkflowTemplate(
    workflow_key="rfq_to_order",
    name="Customer RFQ to Order",
    workflow_type="sales",
    steps=(
        StepDefinition("enquiry", "Customer enquiry"),
        StepDefinition("drawing_review", "Drawing review", depends_on=("enquiry",)),
        StepDefinition(
            "technical_clarification", "Technical clarification", depends_on=("drawing_review",)
        ),
        StepDefinition("costing", "Costing", depends_on=("technical_clarification",)),
        StepDefinition("quotation", "Quotation", depends_on=("costing",)),
        StepDefinition("follow_up", "Customer follow-up", depends_on=("quotation",)),
        StepDefinition("order_confirmation", "Order confirmation", depends_on=("follow_up",)),
        StepDefinition(
            "production_planning", "Production planning", depends_on=("order_confirmation",)
        ),
    ),
)

MAINTENANCE_WORKFLOW = WorkflowTemplate(
    workflow_key="maintenance_workflow",
    name="Maintenance Workflow",
    workflow_type="maintenance",
    steps=(
        StepDefinition("request", "Maintenance request"),
        StepDefinition("registration", "Issue registration", depends_on=("request",)),
        StepDefinition("inspection", "Inspection", depends_on=("registration",)),
        StepDefinition("spare_parts", "Spare-parts requirement", depends_on=("inspection",)),
        StepDefinition("repair", "Repair activity", depends_on=("spare_parts",)),
        StepDefinition("verification", "Verification", depends_on=("repair",)),
        StepDefinition("closure", "Closure", depends_on=("verification",)),
    ),
)

REFERENCE_WORKFLOWS: tuple[WorkflowTemplate, ...] = (
    PURCHASE_TO_PRODUCTION,
    RFQ_TO_ORDER,
    MAINTENANCE_WORKFLOW,
)


__all__ = [
    "MAINTENANCE_WORKFLOW",
    "PURCHASE_TO_PRODUCTION",
    "REFERENCE_WORKFLOWS",
    "RFQ_TO_ORDER",
    "StepDefinition",
    "StepObservation",
    "WorkflowTemplate",
    "compute_observed_steps",
    "current_step",
]
