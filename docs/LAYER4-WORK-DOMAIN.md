# Humisense Brain — Layer 4 Work Domain Model

Canonical schema version `WORK_SCHEMA_VERSION = "4.0"`.
Companion to [`docs/LAYER4-ARCHITECTURE.md`](LAYER4-ARCHITECTURE.md).

This document defines the canonical work model: the entities, their fields, their state
machines, and the rules that keep derived work trustworthy. It is the contract later layers
read.

---

## 1. Source record vs. work entity

A **source record** is what a system captured (an email, a purchase order, a maintenance
ticket). A **work entity** is the real-world unit of work that one or more source records
describe.

The mapping is many-to-many and is never assumed:

* One work entity may be referenced by several source records (an email thread and an ERP
  order that describe the same purchase).
* Several source records may describe the same operational activity; they must not create
  duplicate work entities.
* A work entity may exist with **no** source record when it is created by an authorized
  user.

Duplicate prevention uses a deterministic, tenant-scoped identity key (see §3.2), not a
fuzzy match. When identity is uncertain, Layer 4 records a `WORK_CONFLICT` rather than
merging or duplicating silently.

---

## 2. Work entity

```
work_id                 uuid      primary key
organization_id         uuid      tenant
work_key                text      deterministic identity key (unique per tenant)
work_type               enum      WorkType
title                   text
description             text | null
domain                  enum      WorkDomain (production | procurement | sales | maintenance
                                  | quality | finance | management | general)
department_id           uuid | null   -> departments
owner_id                uuid | null   verified owner (unresolved when null)
assigned_user_id        uuid | null   verified executor (unresolved when null)
creator_id              uuid | null
priority                enum      WorkPriorityLevel (source | user | rule)
priority_source         enum      SOURCE | USER | RULE
status                  enum      WorkStatus
source_status           text | null   the source system's own status, verbatim
origin                  enum      EXPLICIT | EXTRACTED | INFERRED | MANUAL
confidence              float | null  0..1, only for EXTRACTED/INFERRED
start_at                timestamptz | null
due_at                  timestamptz | null
due_source              enum | null   DeadlineType
completed_at            timestamptz | null
parent_work_id          uuid | null   -> work_entities (self)
customer_ref            text | null
supplier_ref            text | null
project_ref             text | null
order_ref               text | null
machine_ref             text | null   matches Layer 3 detection entity_id when verified
production_ref          text | null
correlation_id          text
schema_version          text
created_at, updated_at  timestamptz
```

### 2.1 Work types

`TASK`, `FOLLOW_UP`, `APPROVAL`, `PURCHASE_REQUIREMENT`, `PRODUCTION_REQUIREMENT`,
`MAINTENANCE_ACTIVITY`, `CUSTOMER_COMMITMENT`, `SUPPLIER_COMMITMENT`,
`DELIVERY_OBLIGATION`, `DOCUMENT_REQUEST`, `QUALITY_ISSUE`, `FINANCIAL_REVIEW`,
`OPERATIONAL_ISSUE`, `PROJECT_ACTIVITY`, `GENERAL_WORK_ITEM`.

Extensible: the enum is the canonical set; a tenant-specific label lives in the work
entity's flexible attributes, never by inventing a new canonical value at runtime.

### 2.2 Origin and confidence

| Origin | Meaning | Confidence |
| --- | --- | --- |
| `EXPLICIT` | Recorded as a task by the source system. | 1.0 |
| `EXTRACTED` | Extracted from a communication/document by an upstream extractor. | required (0..1) |
| `INFERRED` | Derived from a structured operational record. | required (0..1) |
| `MANUAL` | Created by an authorized user. | 1.0 |

An inferred or extracted work item is **never** presented as a confirmed assignment. If it
has no verified owner or due date, those fields are null and the state is `UNRESOLVED`.

---

## 3. Identity and provenance

### 3.1 Source references

`work_source_references` links a work entity to the canonical records that describe it:

```
id, organization_id, work_id, canonical_record_id, raw_record_id | null,
source_system, source_record_key, source_record_type,
relationship          enum  PRIMARY | SUPPORTING | MENTIONED
confidence            float | null
created_at
unique (organization_id, work_id, canonical_record_id)
```

`PRIMARY` is the record that created the work entity; `SUPPORTING` corroborates it;
`MENTIONED` merely references it. The distinction is what prevents "a person mentioned in
an email" from becoming an owner.

### 3.2 Work key (deterministic identity)

```
work_key = f"{work_type}:{primary_source_system}:{primary_source_record_key}"
```

for source-derived work, and a user-supplied or generated key for manual work. The key is
unique per tenant. Re-deriving the same source record produces the same key and therefore
an upsert, not a duplicate.

---

## 4. Status model

Canonical statuses: `NEW`, `OPEN`, `ASSIGNED`, `IN_PROGRESS`, `WAITING`, `BLOCKED`,
`ON_HOLD`, `UNDER_REVIEW`, `COMPLETED`, `CANCELLED`, `UNKNOWN`.

### 4.1 Permitted transitions

```
NEW         -> OPEN, ASSIGNED, CANCELLED, UNKNOWN
OPEN        -> ASSIGNED, IN_PROGRESS, WAITING, BLOCKED, ON_HOLD, UNDER_REVIEW, CANCELLED, UNKNOWN
ASSIGNED    -> IN_PROGRESS, WAITING, BLOCKED, ON_HOLD, UNDER_REVIEW, COMPLETED, CANCELLED, UNKNOWN
IN_PROGRESS -> WAITING, BLOCKED, ON_HOLD, UNDER_REVIEW, COMPLETED, CANCELLED, UNKNOWN
WAITING     -> IN_PROGRESS, BLOCKED, ON_HOLD, UNDER_REVIEW, COMPLETED, CANCELLED, UNKNOWN
BLOCKED     -> IN_PROGRESS, WAITING, ON_HOLD, UNDER_REVIEW, COMPLETED, CANCELLED, UNKNOWN
ON_HOLD     -> IN_PROGRESS, WAITING, BLOCKED, UNDER_REVIEW, COMPLETED, CANCELLED, UNKNOWN
UNDER_REVIEW-> IN_PROGRESS, WAITING, BLOCKED, COMPLETED, CANCELLED, UNKNOWN
COMPLETED   -> (terminal; only a manual reopen to IN_PROGRESS by an authorized user)
CANCELLED   -> (terminal)
UNKNOWN     -> OPEN, ASSIGNED, IN_PROGRESS, CANCELLED
```

An illegal transition raises `IllegalWorkTransition`, exactly as Layer 2's processing state
machine does. Terminal states have no automated outgoing transition.

### 4.2 Source status mapping

Source systems do not share status names. `SourceStatusMapping` maps a
`(source_system, source_status)` pair to a canonical `WorkStatus`, per tenant, configured
by an administrator. The original `source_status` string is always preserved on the work
entity.

Unmapped source statuses map to `UNKNOWN`, never to a guessed status.

**Completion is never inferred.** A work item is not marked `COMPLETED` because an email
thread stopped, a source record disappeared, or a timestamp passed. Completion requires an
explicit source status mapped to `COMPLETED`, or an authorized manual action.

---

## 5. Assignments and ownership

`work_assignments` holds every participant and their role:

```
id, organization_id, work_id,
participant_type   enum  USER | TEAM | DEPARTMENT | EXTERNAL
user_id            uuid | null
department_id      uuid | null
external_name      text | null
role               enum  OWNER | ASSIGNEE | REQUESTER | APPROVER | COLLABORATOR |
                         ACCOUNTABLE_MANAGER | ESCALATION
verification       enum  VERIFIED | SOURCE_REPORTED | INFERRED | UNRESOLVED
confidence         float | null
source_reference_id uuid | null
created_at, updated_at
unique (organization_id, work_id, participant_type, coalesce(user_id, department_id, external_name), role)
```

Rules:

* Ownership is explicit and verified, or `UNRESOLVED`. It is never guessed from a name in
  a conversation.
* Ownership changes are recorded in `work_timeline_events` and `work_assignments` history;
  the previous state is never overwritten in the timeline.
* A person mentioned in a message may be recorded as `MENTIONED` in source references, but
  that is not an assignment.

---

## 6. Commitments

`work_commitments`:

```
commitment_id, organization_id, work_id | null,
commitment_type    enum  INTERNAL | CUSTOMER | SUPPLIER | DEPARTMENTAL | DELIVERY |
                         FINANCIAL | OPERATIONAL
description        text
committing_party   text      who made the promise
receiving_party    text      who it was made to
responsible_owner  uuid | null
related_customer_ref, related_supplier_ref, related_order_ref  text | null
promised_at        timestamptz | null   when the promise was made
due_at             timestamptz | null   the promised date
actual_completion_at timestamptz | null
commitment_status  enum  PROPOSED | CONFIRMED | REQUESTED | ESTIMATED | CONTRACTUAL |
                         INFERRED | AT_RISK | MET | MISSED | CANCELLED
verification_status enum VERIFIED | SOURCE_REPORTED | INFERRED | UNRESOLVED
confidence         float | null
source_reference   text | null
created_at, updated_at
```

`work_commitment_history` is append-only: every change to a commitment (date, status,
parties) writes a row preserving previous and new state, so a moved delivery date is
visible, not erased.

Rules:

* A **requested** date is not a **confirmed** promise. `REQUESTED` and `PROPOSED` are
  distinct from `CONFIRMED` and `CONTRACTUAL`.
* An `ESTIMATED` date is not a commitment.
* An `INFERRED` commitment carries a confidence and remains reviewable.

---

## 7. Deadlines

A deadline is a property of a work entity (`due_at`, `due_source`) and, where relevant, of
a commitment. `DeadlineType`: `ABSOLUTE`, `RELATIVE`, `SOURCE_PROVIDED`, `CONTRACTUAL`,
`ESTIMATED`, `REQUESTED`, `RECURRING`.

Derived, deterministic indicators (never stored as truth, always recomputed from
`due_at` and a supplied `as_of`):

| Indicator | Rule |
| --- | --- |
| `overdue` | `due_at < as_of` and status not terminal. |
| `due_today` | `due_at` on the same calendar day as `as_of` (UTC). |
| `due_soon` | `0 <= due_at - as_of <= due_soon_hours` (policy, default 24h). |

The `as_of` is passed explicitly, so a historical query is judged against the requested
window, not the wall clock. This mirrors Layer 3's freshness rule and avoids marking every
backfill stale.

Event dates, meeting dates, and mentioned dates are **not** deadlines. Only a date the
source presents as a due/required/committed date becomes one, and its type is recorded.

---

## 8. Dependencies

`work_dependencies`:

```
id, organization_id,
source_work_id     uuid      the work that is affected
target_work_id     uuid | null  another work entity
target_entity_type text | null  DEPARTMENT | SUPPLIER | CUSTOMER | MATERIAL | APPROVAL |
                                MACHINE | DOCUMENT | EXTERNAL_MILESTONE
target_entity_ref  text | null  the external identifier
relationship       enum  BLOCKS | BLOCKED_BY | REQUIRES | WAITING_FOR | PRECEDES |
                         RELATED_TO | APPROVAL_REQUIRED | MATERIAL_DEPENDENCY |
                         INFORMATION_REQUIRED
status             enum  OPEN | SATISFIED | UNKNOWN
origin             enum  EXPLICIT | INFERRED | MANUAL
confidence         float | null
verification_status enum VERIFIED | SOURCE_REPORTED | INFERRED | UNRESOLVED
created_at, updated_at
unique (organization_id, source_work_id, relationship, coalesce(target_work_id, target_entity_ref))
```

Rules:

* Explicit dependencies use deterministic rules; inferred dependencies are labelled
  `INFERRED` and remain reviewable.
* Dependency chains and circular dependencies are detected deterministically and reported
  as conflicts (`CIRCULAR_DEPENDENCY`); cycles are not "resolved" automatically.
* An unverified relationship never reassigns work or changes operational status.

---

## 9. Blockers

`work_blockers`:

```
blocker_id, organization_id, affected_work_id,
blocker_type       enum  EXTERNAL | INTERNAL | DEPENDENCY | INFORMATION | APPROVAL |
                         MATERIAL | CAPACITY | UNKNOWN
description        text
blocking_entity    text | null
responsible_department_id uuid | null
detected_at        timestamptz
expected_resolution_at timestamptz | null
blocker_status     enum  OPEN | RESOLVED | UNKNOWN
evidence_references jsonb   references only, never payloads
provenance         enum  EXPLICIT | INFERRED
verification_status enum VERIFIED | SOURCE_REPORTED | INFERRED | UNRESOLVED
created_at, updated_at
```

Rule: Layer 4 does not claim to know **why** something is blocked unless supporting
evidence exists. When the reason is unknown, `blocker_type = UNKNOWN` and
`verification_status = UNRESOLVED`.

---

## 10. Follow-ups

`work_followups`:

```
follow_up_id, organization_id, related_work_id,
target_type        enum  PERSON | DEPARTMENT | SUPPLIER | CUSTOMER | INTERNAL
target_ref         text
owner_id           uuid | null
reason             text
last_activity_at   timestamptz | null
next_due_at        timestamptz | null
urgency            enum  LOW | NORMAL | HIGH | URGENT
follow_up_status   enum  SUGGESTED | OPEN | OVERDUE | DONE | CANCELLED
rule_id            text      the deterministic rule that produced it
provenance         enum  RULE | EXPLICIT
created_at, updated_at
```

Rules use documented, deterministic inputs: last interaction timestamp, explicit response
deadline, work status, commitment date, dependency status. A `SUGGESTED` follow-up is
distinct from an `OVERDUE` one. Layer 4 never sends anything to an external party.

---

## 11. Workflows

`workflow_definitions` (tenant-configurable, versioned):

```
id, organization_id, workflow_key, version, name, workflow_type, department_id | null,
entry_conditions jsonb, exit_conditions jsonb, steps jsonb, status_mappings jsonb,
created_at
unique (organization_id, workflow_key, version)
```

`steps` is an ordered list of `{step_key, name, responsible_role, depends_on[],
expected_duration_seconds | null, source_status_hint}`. `workflow_instances` binds a work
entity (or a correlation group) to a definition version; `workflow_steps` records observed
step state per instance.

Rules:

* **Observed** state is distinct from **expected** state. The observed state is derived
  from verified source statuses and dependencies; the expected state is the definition.
* A step is never advanced by elapsed time alone.
* No manufacturing workflow is hardcoded in the engine; the three reference workflows in
  the brief are shipped as **optional seed definitions**, not as engine logic.

---

## 12. Timeline

`work_timeline_events` (append-only):

```
event_id, organization_id, work_id,
event_type         text   work.created, work.assigned, work.status_changed,
                          commitment.created, commitment.revised, deadline.changed,
                          department.updated, dependency.created, blocker.identified,
                          followup.created, work.completed, source.updated
event_at           timestamptz  when the event actually occurred
source_timestamp   timestamptz | null  the source's own timestamp
ingested_at        timestamptz  when Layer 4 read the source
processed_at       timestamptz  when Layer 4 processed it
actor_id           uuid | null
actor_type         text   user | system | worker | source
previous_state     jsonb | null
new_state          jsonb | null
source_reference   jsonb | null
provenance         enum   EXPLICIT | EXTRACTED | INFERRED | MANUAL
correlation_id     text
created_at
```

Four distinct times, never conflated, exactly as Layer 2's contract requires. History is
never overwritten: a correction writes a new event.

---

## 13. Prioritization metadata

`work_priorities` (recomputed, not authoritative):

```
id, organization_id, work_id,
level              enum  LOW | NORMAL | HIGH | URGENT | CRITICAL
source             enum  SOURCE | USER | RULE
overdue            bool
due_soon           bool
blocked            bool
dependency_count   int
waiting_duration_seconds  int | null
commitment_status  text | null
source_completeness float 0..1  how many canonical fields are populated
related_detection_ids jsonb
computed_at        timestamptz
unique (organization_id, work_id, source)
```

Rule: a `RULE`-derived level never overwrites a `USER`-assigned level. The API returns all
sources side by side; the UI shows the human value as authoritative and the rule value as
an indicator. Layer 4 does not present a deterministic urgency score as a business
decision.

---

## 14. Conflicts

`work_conflicts`:

```
id, organization_id, work_id | null, entity_type, entity_key,
conflict_type      enum  DUPLICATE_WORK | CONFLICTING_OWNER | CONFLICTING_DEADLINE |
                         INCONSISTENT_STATUS | MISSING_REFERENCE | UNRESOLVED_IDENTITY |
                         AMBIGUOUS_ORGANIZATION | INCONSISTENT_PARTY_ID |
                         CONTRADICTORY_COMMITMENT | STALE_SOURCE |
                         SOURCE_SYNC_CONFLICT | CIRCULAR_DEPENDENCY
detail             text
candidates         jsonb   the competing values, each with its source
status             enum  OPEN | RESOLVED | ACCEPTED
resolved_value     jsonb | null
resolved_by        uuid | null
resolution_note    text | null
detected_at        timestamptz
resolved_at        timestamptz | null
```

Rule: a conflict is **never** silently resolved. The latest timestamp is not assumed to be
the truth unless an explicit source-authority policy (per tenant, per conflict type) says
so. Resolution is a recorded, auditable action.

---

## 15. Extractions

`work_extractions` records candidate facts received from an approved upstream extractor:

```
id, organization_id, source_record_reference, extraction_method, extraction_version,
candidate_type     enum  TASK | COMMITMENT | DEADLINE | DEPENDENCY | OWNER | BLOCKER
fields             jsonb   the extracted field values
confidence         float
verification_status enum  UNVERIFIED | VERIFIED | REJECTED
promoted_work_id   uuid | null   set when an authorized action promoted it
created_at
```

An extraction is a **candidate**. It becomes work only through a deterministic promotion
path or an authorized action; it is never auto-assigned. `UNVERIFIED` extractions are
visible but never presented as confirmed facts.

---

## 16. Read models

`views.py` builds three read models over the same tables. None of them is a separate store;
all are tenant-scoped and authorization-filtered.

* **Personal:** work owned by / assigned to the principal, work waiting for them, work they
  are waiting for, their commitments, their follow-ups, their department's work.
* **Manager:** department workload, open/overdue/blocked work, pending approvals, work
  awaiting other departments, upcoming deadlines, supplier/customer commitments.
* **Executive:** cross-department critical work, important customer commitments, major
  delivery risks **supported by evidence**, persistent blockers, overdue high-priority
  work, pending management approvals.

No performance score, productivity ranking, or fabricated metric is produced. Counts of
messages or tasks are never presented as a measure of an employee's productivity.
