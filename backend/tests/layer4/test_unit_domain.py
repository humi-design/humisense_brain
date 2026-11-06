"""Layer 4 unit tests: the pure, deterministic logic.

Nothing here touches the database. These tests exist because the derivation, status,
deadline, dependency, blocker, follow-up, conflict, and priority rules are the part of Layer 4
that must be reproducible: a reviewer must be able to check the output by reading the rule.
The tests also pin the boundaries the brief cares about most — no invented owner, no invented
deadline, no inferred completion, no silently resolved conflict.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from humisense.db.models import (
    BlockerType,
    CommitmentStatus,
    DeadlineType,
    DependencyRelationship,
    ExtractionCandidateType,
    FollowUpStatus,
    FollowUpTargetType,
    ParticipantType,
    PriorityLevel,
    Urgency,
    VerificationStatus,
    WorkConflictType,
    WorkDomain,
    WorkflowStepState,
    WorkOrigin,
    WorkStatus,
    WorkType,
)
from humisense.layer4 import dependencies, followups, ownership, priorities, workflows
from humisense.layer4.blockers import blocker_from_dependency, unknown_blocker
from humisense.layer4.conflicts import ConflictingValue, detect_value_conflict, resolve_with_policy
from humisense.layer4.contract import (
    MAX_ATTRIBUTE_KEYS,
    MAX_EVIDENCE_REFS,
    WorkBlocker,
    WorkEntity,
)
from humisense.layer4.deadlines import (
    assess_deadline,
    coerce_aware,
    is_approaching,
    shift,
    waiting_duration,
)
from humisense.layer4.departments import build_department_mapping, resolve_domain
from humisense.layer4.derivation import (
    SourceRecordView,
    derive_work,
    resolve_due_date,
    work_key_for,
)
from humisense.layer4.errors import IllegalWorkTransition
from humisense.layer4.extraction import (
    decide_promotion,
    is_untrusted_source,
    sanitize_extraction,
)
from humisense.layer4.policy import WorkPolicy
from humisense.layer4.status import (
    SourceStatusMapping,
    assert_transition,
    can_transition,
    is_terminal,
    normalize_source_status,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


# --- Status model ---------------------------------------------------------------


def test_source_status_mapping_is_conservative() -> None:
    mapping = SourceStatusMapping()
    assert mapping.map(source_system="erp", source_status="In Progress") == WorkStatus.IN_PROGRESS
    assert mapping.map(source_system="erp", source_status="waiting for") == WorkStatus.WAITING
    # An unknown vocabulary maps to UNKNOWN, never to a guess.
    assert mapping.map(source_system="erp", source_status="snoozed") == WorkStatus.UNKNOWN
    assert mapping.map(source_system="erp", source_status=None) == WorkStatus.UNKNOWN


def test_a_stopped_conversation_is_not_completion() -> None:
    """No non-completion source status may map to COMPLETED."""
    mapping = SourceStatusMapping()
    for text in ("new", "open", "assigned", "in_progress", "waiting", "blocked", "on_hold"):
        assert mapping.map(source_system="s", source_status=text) != WorkStatus.COMPLETED


def test_source_status_override_beats_default() -> None:
    mapping = SourceStatusMapping(
        overrides={("legacy_erp", "closed"): WorkStatus.CANCELLED},
    )
    assert mapping.map(source_system="legacy_erp", source_status="closed") == WorkStatus.CANCELLED
    assert mapping.map(source_system="other", source_status="closed") == WorkStatus.COMPLETED


def test_normalize_source_status_collapses_separators() -> None:
    assert normalize_source_status(" In-Progress ") == "in_progress"
    assert normalize_source_status("in progress") == "in_progress"
    assert normalize_source_status(None) == ""


def test_transition_graph_is_closed() -> None:
    assert can_transition(WorkStatus.OPEN, WorkStatus.IN_PROGRESS)
    assert can_transition(WorkStatus.IN_PROGRESS, WorkStatus.COMPLETED)
    assert not can_transition(WorkStatus.COMPLETED, WorkStatus.NEW)
    assert not can_transition(WorkStatus.CANCELLED, WorkStatus.OPEN)
    # A self-transition is not a change.
    assert not can_transition(WorkStatus.OPEN, WorkStatus.OPEN)


def test_illegal_transition_raises() -> None:
    with pytest.raises(IllegalWorkTransition):
        assert_transition(WorkStatus.COMPLETED, WorkStatus.OPEN)
    assert_transition(WorkStatus.IN_PROGRESS, WorkStatus.COMPLETED)


def test_terminal_statuses() -> None:
    assert is_terminal(WorkStatus.COMPLETED)
    assert is_terminal(WorkStatus.CANCELLED)
    assert not is_terminal(WorkStatus.BLOCKED)


# --- Deadlines ------------------------------------------------------------------


def test_overdue_is_computed_from_the_reference_time() -> None:
    assessment = assess_deadline(due_at=BASE - timedelta(hours=1), as_of=BASE, is_terminal=False)
    assert assessment.overdue is True
    assert assessment.seconds_until_due == -3600


def test_a_historical_window_is_not_reported_overdue() -> None:
    """A backfill judges against the window end, not the wall clock."""
    due = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assessment = assess_deadline(due_at=due, as_of=BASE, is_terminal=False)
    assert assessment.overdue is False
    # It is still "due soon" because the reference time is the window start, not "now";
    # the point is that overdue is judged against the passed reference, not the wall clock.
    assert assessment.due_soon is True
    # Six hours before the reference it is neither overdue nor imminent.
    earlier = assess_deadline(due_at=due, as_of=due - timedelta(hours=30), is_terminal=False)
    assert earlier.overdue is False
    assert earlier.due_soon is False


def test_completed_work_is_never_overdue() -> None:
    assessment = assess_deadline(due_at=BASE - timedelta(days=5), as_of=BASE, is_terminal=True)
    assert assessment.overdue is False


def test_due_soon_window() -> None:
    assert is_approaching(due_at=BASE + timedelta(hours=3), as_of=BASE, within_hours=24)
    assert not is_approaching(due_at=BASE - timedelta(hours=1), as_of=BASE, within_hours=24)
    assert not is_approaching(due_at=None, as_of=BASE)


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError):
        coerce_aware(datetime(2026, 1, 1))
    assert coerce_aware(None) is None


def test_waiting_duration_is_non_negative() -> None:
    assert waiting_duration(since=BASE - timedelta(hours=2), as_of=BASE) == 7200
    assert waiting_duration(since=BASE + timedelta(hours=2), as_of=BASE) == 0
    assert waiting_duration(since=None, as_of=BASE) is None


def test_relative_shift() -> None:
    assert shift(BASE, days=1, hours=2) == BASE + timedelta(days=1, hours=2)


def test_only_an_explicit_due_key_is_a_deadline() -> None:
    """A meeting date or a generic date is not a deadline."""
    assert resolve_due_date({"date": "2026-02-01T00:00:00+00:00"}) == (None, None)
    due, kind = resolve_due_date({"due_at": "2026-02-01T00:00:00+00:00"})
    assert due == datetime(2026, 2, 1, tzinfo=UTC)
    assert kind == DeadlineType.SOURCE_PROVIDED


def test_naive_source_date_is_ignored() -> None:
    assert resolve_due_date({"due_at": "2026-02-01T00:00:00"}) == (None, None)


# --- Ownership ------------------------------------------------------------------


class _Directory:
    def __init__(self, known: dict[str, uuid.UUID]) -> None:
        self._known = known

    async def resolve(self, organization_id: uuid.UUID, identity: str) -> uuid.UUID | None:
        return self._known.get(identity)


async def test_a_mentioned_name_is_not_an_owner() -> None:
    """A sender/participant is a mention, not an assignment."""
    directory = _Directory({"alice": uuid.uuid4()})
    payload = {"from": "alice", "to": ["bob"], "title": "Re: drawing"}
    owner = await ownership.resolve_owner(
        organization_id=uuid.uuid4(), payload=payload, directory=directory
    )
    assert owner is None
    assert ownership.mentioned_identities(payload) == ["alice", "bob"]


async def test_explicit_owner_field_is_resolved() -> None:
    user_id = uuid.uuid4()
    directory = _Directory({"alice": user_id})
    owner = await ownership.resolve_owner(
        organization_id=uuid.uuid4(), payload={"assignee": "alice"}, directory=directory
    )
    assert owner is not None
    assert owner.user_id == user_id
    assert owner.participant_type is ParticipantType.USER
    assert owner.verification is VerificationStatus.VERIFIED


async def test_unknown_owner_stays_external_and_source_reported() -> None:
    owner = await ownership.resolve_owner(
        organization_id=uuid.uuid4(),
        payload={"owner": "supplier@example.com"},
        directory=_Directory({}),
    )
    assert owner is not None
    assert owner.user_id is None
    assert owner.participant_type is ParticipantType.EXTERNAL
    assert owner.verification is VerificationStatus.SOURCE_REPORTED


async def test_no_owner_field_returns_none() -> None:
    assert (
        await ownership.resolve_owner(
            organization_id=uuid.uuid4(), payload={"title": "x"}, directory=None
        )
        is None
    )


# --- Derivation -----------------------------------------------------------------


def _record(
    *,
    record_type: str = "task",
    source_system: str = "erp",
    source_record_key: str = "TASK-1",
    payload: dict[str, object] | None = None,
) -> SourceRecordView:
    return SourceRecordView(
        canonical_record_id=uuid.uuid4(),
        raw_record_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        record_type=record_type,
        entity_type=record_type,
        source_record_key=source_record_key,
        source_timestamp=BASE,
        event_timestamp=BASE,
        observed_timestamp=BASE,
        received_timestamp=BASE,
        normalized_payload=payload or {},
        extensions={},
        record_metadata={},
        source_system=source_system,
        correlation_id="corr",
    )


async def test_non_derivable_record_type_yields_nothing() -> None:
    """An email is not work unless the tenant configured it as a work source."""
    derivation = await derive_work(
        _record(record_type="email", payload={"subject": "hi"}),
        policy=WorkPolicy(),
        status_mapping=SourceStatusMapping(),
    )
    assert derivation is None


async def test_explicit_task_derivation() -> None:
    derivation = await derive_work(
        _record(payload={"title": "Replace bearing", "status": "open"}),
        policy=WorkPolicy(),
        status_mapping=SourceStatusMapping(),
    )
    assert derivation is not None
    assert derivation.work_type is WorkType.TASK
    assert derivation.origin is WorkOrigin.EXPLICIT
    assert derivation.confidence == 1.0
    assert derivation.status is WorkStatus.OPEN
    assert derivation.owner_id is None  # no owner stated -> unresolved, never guessed
    assert derivation.due_at is None


async def test_derivation_work_key_is_stable_across_runs() -> None:
    record = _record(payload={"title": "x"})
    first = await derive_work(record, policy=WorkPolicy(), status_mapping=SourceStatusMapping())
    second = await derive_work(record, policy=WorkPolicy(), status_mapping=SourceStatusMapping())
    assert first is not None and second is not None
    assert first.work_key == second.work_key
    assert first.work_key == work_key_for(
        work_type=WorkType.TASK, source_system="erp", source_record_key="TASK-1"
    )


async def test_a_requested_date_becomes_a_requested_deadline_not_a_confirmed_one() -> None:
    derivation = await derive_work(
        _record(
            record_type="supplier_commitment",
            payload={"title": "Delivery", "requested_due_at": "2026-03-01T00:00:00+00:00"},
        ),
        policy=WorkPolicy(),
        status_mapping=SourceStatusMapping(),
    )
    assert derivation is not None
    assert derivation.due_source is DeadlineType.REQUESTED
    assert derivation.commitments
    assert derivation.commitments[0].commitment_status is CommitmentStatus.PROPOSED


async def test_explicit_dependency_fields_are_derived() -> None:
    derivation = await derive_work(
        _record(
            payload={
                "title": "Assemble",
                "blocked_by": ["PO-77"],
                "waiting_for": "customer drawing",
                "material_dependency": "steel plate",
            }
        ),
        policy=WorkPolicy(),
        status_mapping=SourceStatusMapping(),
    )
    assert derivation is not None
    relationships = {d.relationship for d in derivation.dependencies}
    assert DependencyRelationship.BLOCKED_BY in relationships
    assert DependencyRelationship.WAITING_FOR in relationships
    assert DependencyRelationship.MATERIAL_DEPENDENCY in relationships
    assert all(d.origin is WorkOrigin.EXPLICIT for d in derivation.dependencies)


async def test_department_is_resolved_from_an_explicit_field_only() -> None:
    department_id = uuid.uuid4()
    mapping = build_department_mapping([("production", department_id, WorkDomain.PRODUCTION)])
    # Free text mentioning "production" does not assign the department.
    derivation = await derive_work(
        _record(payload={"title": "production line issue"}),
        policy=WorkPolicy(),
        status_mapping=SourceStatusMapping(),
        department_mapping=mapping,
    )
    assert derivation is not None
    assert derivation.department_id is None
    # An explicit field does.
    derivation = await derive_work(
        _record(payload={"title": "x", "department": "Production"}),
        policy=WorkPolicy(),
        status_mapping=SourceStatusMapping(),
        department_mapping=mapping,
    )
    assert derivation is not None
    assert derivation.department_id == department_id


def test_resolve_domain_prefers_explicit_field() -> None:
    assert resolve_domain({"domain": "quality"}, record_kind="task") is WorkDomain.QUALITY
    assert resolve_domain({}, record_kind="maintenance") is WorkDomain.MAINTENANCE
    assert resolve_domain({}, record_kind="unknown-kind") is WorkDomain.GENERAL


async def test_attributes_are_bounded() -> None:
    payload = {"title": "x"} | {f"k{i}": i for i in range(200)}
    derivation = await derive_work(
        _record(payload=payload), policy=WorkPolicy(), status_mapping=SourceStatusMapping()
    )
    assert derivation is not None
    assert len(derivation.attributes) <= 20  # only the whitelisted keys are kept


# --- Contract validation --------------------------------------------------------


def test_work_entity_contract_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError):
        WorkEntity(
            work_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            work_key="k",
            work_type="task",
            title="t",
            domain="general",
            status="open",
            correlation_id="c",
            created_at=datetime(2026, 1, 1),
            updated_at=BASE,
        )


def test_work_entity_contract_bounds_attributes() -> None:
    with pytest.raises(ValueError):
        WorkEntity(
            work_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            work_key="k",
            work_type="task",
            title="t",
            domain="general",
            status="open",
            correlation_id="c",
            attributes={f"k{i}": i for i in range(MAX_ATTRIBUTE_KEYS + 1)},
            created_at=BASE,
            updated_at=BASE,
        )


def test_blocker_contract_bounds_evidence() -> None:
    with pytest.raises(ValueError):
        WorkBlocker(
            blocker_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            affected_work_id=uuid.uuid4(),
            description="blocked",
            detected_at=BASE,
            correlation_id="c",
            evidence_references=[{} for _ in range(MAX_EVIDENCE_REFS + 1)],
        )


# --- Dependencies ---------------------------------------------------------------


def _edge(source: uuid.UUID, target: uuid.UUID, relationship: DependencyRelationship) -> object:
    return dependencies.DependencyEdge(
        source_work_id=source, target_work_id=target, relationship=relationship
    )


def test_cycle_is_detected() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cycles = dependencies.find_cycles(
        [
            _edge(a, b, DependencyRelationship.BLOCKS),
            _edge(b, c, DependencyRelationship.BLOCKS),
            _edge(c, a, DependencyRelationship.BLOCKS),
        ]
    )
    assert len(cycles) == 1
    assert set(cycles[0]) == {a, b, c}


def test_related_to_is_not_a_cycle() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    cycles = dependencies.find_cycles(
        [
            _edge(a, b, DependencyRelationship.RELATED_TO),
            _edge(b, a, DependencyRelationship.RELATED_TO),
        ]
    )
    assert cycles == []


def test_chain_length_is_bounded() -> None:
    ids = [uuid.uuid4() for _ in range(5)]
    edges = [
        _edge(ids[i], ids[i + 1], DependencyRelationship.PRECEDES) for i in range(len(ids) - 1)
    ]
    assert dependencies.dependency_chain_length(edges) == 5


# --- Blockers -------------------------------------------------------------------


def test_a_satisfied_dependency_is_not_a_blocker() -> None:
    assert (
        blocker_from_dependency(
            relationship=DependencyRelationship.BLOCKED_BY,
            dependency_status="satisfied",
            target_ref="PO-1",
            evidence=[],
            detected_at=BASE,
        )
        is None
    )


def test_open_dependency_derives_a_blocker_with_unresolved_cause() -> None:
    blocker = blocker_from_dependency(
        relationship=DependencyRelationship.MATERIAL_DEPENDENCY,
        dependency_status="open",
        target_ref="steel plate",
        evidence=[{"canonical_record_id": str(uuid.uuid4())}],
        detected_at=BASE,
    )
    assert blocker is not None
    assert blocker.blocker_type is BlockerType.MATERIAL
    assert blocker.verification_status is VerificationStatus.UNRESOLVED
    assert blocker.provenance is WorkOrigin.INFERRED


def test_unknown_blocker_is_honest() -> None:
    blocker = unknown_blocker(detected_at=BASE, evidence=[])
    assert blocker.blocker_type is BlockerType.UNKNOWN
    assert blocker.verification_status is VerificationStatus.UNRESOLVED


# --- Follow-ups -----------------------------------------------------------------


def test_followup_requires_an_explicit_reference_time() -> None:
    inputs = followups.FollowUpInputs(
        related_work_id=uuid.uuid4(),
        target_type=FollowUpTargetType.SUPPLIER,
        target_ref="ACME",
    )
    with pytest.raises(ValueError):
        followups.evaluate_followups(inputs)


def test_overdue_response_deadline_produces_an_overdue_followup() -> None:
    inputs = followups.FollowUpInputs(
        related_work_id=uuid.uuid4(),
        target_type=FollowUpTargetType.SUPPLIER,
        target_ref="ACME",
        response_due_at=BASE - timedelta(hours=2),
        as_of=BASE,
    )
    candidates = followups.evaluate_followups(inputs)
    assert len(candidates) == 1
    assert candidates[0].rule_id == followups.RULE_RESPONSE_OVERDUE
    assert candidates[0].status is FollowUpStatus.OVERDUE
    assert candidates[0].urgency is Urgency.URGENT


def test_silence_within_window_produces_no_followup() -> None:
    inputs = followups.FollowUpInputs(
        related_work_id=uuid.uuid4(),
        target_type=FollowUpTargetType.CUSTOMER,
        target_ref="CUST",
        last_activity_at=BASE - timedelta(hours=1),
        as_of=BASE,
    )
    assert followups.evaluate_followups(inputs) == []


def test_no_response_after_window_suggests_a_followup() -> None:
    inputs = followups.FollowUpInputs(
        related_work_id=uuid.uuid4(),
        target_type=FollowUpTargetType.SUPPLIER,
        target_ref="ACME",
        last_activity_at=BASE - timedelta(hours=100),
        as_of=BASE,
    )
    candidates = followups.evaluate_followups(inputs)
    assert [c.rule_id for c in candidates] == [followups.RULE_NO_RESPONSE]
    # A suggestion is not an overdue item.
    assert candidates[0].status is FollowUpStatus.SUGGESTED


def test_blocked_too_long_suggests_a_followup() -> None:
    inputs = followups.FollowUpInputs(
        related_work_id=uuid.uuid4(),
        target_type=FollowUpTargetType.INTERNAL,
        target_ref="dept",
        work_status="blocked",
        blocked_since=BASE - timedelta(days=30),
        as_of=BASE,
    )
    rule_ids = {c.rule_id for c in followups.evaluate_followups(inputs)}
    assert followups.RULE_BLOCKED_TOO_LONG in rule_ids


# --- Conflicts ------------------------------------------------------------------


def test_two_sources_reporting_the_same_value_is_not_a_conflict() -> None:
    conflict = detect_value_conflict(
        conflict_type=WorkConflictType.CONFLICTING_DEADLINE,
        entity_type="work_entity",
        entity_key="k",
        field_name="due_at",
        values=[
            ConflictingValue("erp", "PO-1", "2026-02-01"),
            ConflictingValue("email", "msg-9", "2026-02-01"),
        ],
    )
    assert conflict is None


def test_disagreeing_sources_produce_a_conflict() -> None:
    conflict = detect_value_conflict(
        conflict_type=WorkConflictType.CONFLICTING_DEADLINE,
        entity_type="work_entity",
        entity_key="k",
        field_name="due_at",
        values=[
            ConflictingValue("erp", "PO-1", "2026-02-01"),
            ConflictingValue("email", "msg-9", "2026-02-05"),
        ],
    )
    assert conflict is not None
    assert len(conflict.candidates) == 2


def test_no_policy_means_no_automatic_resolution() -> None:
    conflict = detect_value_conflict(
        conflict_type=WorkConflictType.CONFLICTING_OWNER,
        entity_type="work_entity",
        entity_key="k",
        field_name="owner_id",
        values=[
            ConflictingValue("erp", "PO-1", "alice"),
            ConflictingValue("email", "msg-9", "bob"),
        ],
    )
    assert conflict is not None
    assert resolve_with_policy(conflict, policy=None) is None
    # An explicit authority policy selects a candidate, but only explicitly.
    chosen = resolve_with_policy(conflict, policy={WorkConflictType.CONFLICTING_OWNER: "erp"})
    assert chosen is not None and chosen.value == "alice"


# --- Priorities -----------------------------------------------------------------


def test_a_rule_never_overrides_a_user_priority() -> None:
    inputs = priorities.PriorityInputs(
        overdue=True, user_priority=PriorityLevel.LOW, source_priority=PriorityLevel.NORMAL
    )
    assert priorities.effective_priority(inputs) is PriorityLevel.LOW


def test_source_priority_outranks_the_rule() -> None:
    inputs = priorities.PriorityInputs(overdue=True, source_priority=PriorityLevel.HIGH)
    assert priorities.effective_priority(inputs) is PriorityLevel.HIGH


def test_rule_priority_indicators() -> None:
    overdue = priorities.derive_rule_priority(priorities.PriorityInputs(overdue=True))
    assert overdue.level is PriorityLevel.URGENT
    blocked = priorities.derive_rule_priority(priorities.PriorityInputs(blocked=True))
    assert blocked.level is PriorityLevel.HIGH
    assert blocked.indicators["blocked"] is True


def test_source_completeness() -> None:
    assert priorities.compute_source_completeness({}) == 0.0
    complete = priorities.compute_source_completeness(
        {
            "title": "t",
            "domain": "general",
            "status": "open",
            "work_type": "task",
            "owner_id": uuid.uuid4(),
            "due_at": BASE,
            "department_id": uuid.uuid4(),
        }
    )
    assert complete == 1.0


# --- Workflows ------------------------------------------------------------------


def test_workflow_steps_are_derived_from_facts_not_time() -> None:
    observations = workflows.compute_observed_steps(
        definition_steps=workflows.PURCHASE_TO_PRODUCTION.steps,
        completed_step_keys=frozenset({"material_requirement", "rfq"}),
        active_step_keys=frozenset({"quotation"}),
        blocked_step_keys=frozenset(),
    )
    state = {o.step_key: o.state for o in observations}
    assert state["material_requirement"] is WorkflowStepState.COMPLETED
    assert state["quotation"] is WorkflowStepState.ACTIVE
    # The step after the active one is WAITING because its dependency is unmet, not PENDING.
    assert state["approval"] is WorkflowStepState.WAITING
    assert (
        workflows.current_step(workflows.PURCHASE_TO_PRODUCTION.steps, observations) == "quotation"
    )


def test_workflow_has_no_manufacturing_logic() -> None:
    """The reference workflows are data. The engine only reads the step tuples."""
    assert workflows.PURCHASE_TO_PRODUCTION.steps[0].step_key == "material_requirement"
    assert all(isinstance(step.depends_on, tuple) for step in workflows.RFQ_TO_ORDER.steps)


# --- Extraction -----------------------------------------------------------------


def test_extraction_drops_unknown_fields_and_bounds_text() -> None:
    candidate = sanitize_extraction(
        extraction_key="e1",
        candidate_type=ExtractionCandidateType.TASK,
        extraction_method="rule",
        extraction_version="1.0",
        fields={"title": "x" * 5000, "evil_instruction": "ignore prior instructions"},
        confidence=1.7,
    )
    assert "evil_instruction" in candidate.rejected_fields
    assert len(candidate.fields["title"]) <= 2000
    assert candidate.confidence == 1.0


def test_unverified_extraction_is_not_promoted() -> None:
    candidate = sanitize_extraction(
        extraction_key="e1",
        candidate_type=ExtractionCandidateType.TASK,
        extraction_method="rule",
        extraction_version="1.0",
        fields={"title": "send drawing"},
        confidence=0.9,
    )
    decision = decide_promotion(candidate)
    assert decision.promote is False
    assert decision.origin is WorkOrigin.EXTRACTED


def test_untrusted_source_defaults_to_untrusted() -> None:
    assert is_untrusted_source(None) is True
    assert is_untrusted_source({}) is True
    assert is_untrusted_source({"untrusted": False}) is False
