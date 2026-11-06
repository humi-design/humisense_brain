# Humisense Brain — Layer 4: Work Intelligence Engine

Status: architecture and implementation.
Built on Layer 1 (`v0.1.0-rc.1`), Layer 2 (canonical schema `2.0`), and Layer 3
(analytics schema `3.0`).
Audience: engineering, enterprise architecture, business-process owners, security review,
platform operations.

Layer 4 understands how work moves through an organization: what work exists, who owns it,
which department is responsible, what has been committed, what is overdue, what is blocked,
what depends on what, and what requires follow-up. It is a **work-understanding** layer. It
does not remember, connect, retrieve evidence, reason, plan, or act.

---

## 0. Pre-implementation findings (what actually exists)

The repository was inspected before any Layer 4 code was written. The table below records
facts verified in the repository, not assumptions from the brief.

| Area | Verified reality |
| --- | --- |
| Layer 1 status | Released `v0.1.0-rc.1`. Connector SDK types in `connectors/types.py`; `source_records` preserved verbatim. |
| Layer 2 status | Canonical contract frozen at `CANONICAL_SCHEMA_VERSION = "2.0"` (`layer2/contract.py`). Tables `ingest_batches`, `raw_records`, `canonical_records`, `record_quarantine`, `outbox_events`, `record_processing_events` (migration `0002_layer2`). |
| Layer 2 input shape | `canonical_records`: `organization_id`, `connection_id`, `idempotency_key`, `record_type`, `entity_type`, `event_type`, `data_family`, five timestamps (`source`, `event`, `observed`, `received`, `processed`), `normalized_payload` JSONB, `extensions` JSONB, `record_metadata` JSONB, `quality_code`, `schema_version`, `processing_state`, `superseded_by`. |
| Layer 2 business shape | Business normalizer output is `{record_kind, identifier, text, party}` with `data_family="business"`; **every other source field is preserved opaquely in `extensions`**. There is no fixed ERP/CRM/procurement schema anywhere in the repository. |
| Layer 2 industrial shape | Historian normalizer output `{asset_id, tag, value, unit, quality_code}`, `data_family="timeseries"`, `entity_type` `"asset"`/`"tag"`; units canonicalized with an auditable `unit_conversions` record. |
| Layer 3 status | Implemented. `detection_events` + `detection_event_history` (migration `0003_layer3`). Contract `layer3/contract.py`, `ANALYTICS_SCHEMA_VERSION = "3.0"`. |
| Layer 3 detection shape | `DetectionEvent`: `detection_id`, `tenant_id`, `domain`, `source_system`, `connection_id`, `entity_id`, `entity_type`, `metric`, `detection_category`, `detection_type`, `severity`, values, window, `detected_at`, algorithm/model/baseline versions, `anomaly_score`, `confidence`, `data_quality_status`, `evidence_references[]` (references, never payloads), `explanation`, `limitations[]`, `correlation_id`, `schema_version`. |
| Layer 3 → Layer 4 publication | **Absent.** `LAYER3-ARCHITECTURE.md` §10 states the Layer 2 outbox is keyed to a canonical record (NOT NULL FK), so detections are read back through the API rather than pushed downstream. |
| Layer 2 → Layer 4 publication | `outbox_events` exists and is transactional, but its default `layer2_publish_mode` is `disabled`, and its consumer is an external HTTP sink — not an internal Layer 4 projection. |
| Incremental read pattern | Layer 3 already reads Layer 2 incrementally: `AnalyticsRepository.read_canonical_window(...)` bounded by `since`/`until`/`limit`, ordered by `coalesce(event_timestamp, source_timestamp)`, plus `latest_watermark(...)` from `analytics_jobs.watermark` (`tests/layer3/test_engine.py` covers it). |
| Tenant isolation | `db/rls.py`: `ENABLE` + `FORCE` RLS, policy `organization_id = app_current_org()` on a transaction-local GUC. `db/session.py`: `tenant_session` / `system_session`. The app role is `NOSUPERUSER NOBYPASSRLS`. |
| Authorization | `security/permissions.py` deny-by-default role matrix (viewer/operator/admin/owner), enforced in the API dependency layer and in services. |
| Queue & workers | `arq` over Redis. `WorkerSettings` uses **class attributes** (arq reads `settings_cls.__dict__`). Existing cron jobs: stale-job reaper, Layer 2 outbox drain, Layer 3 sweep. |
| Audit | `audit_events`, append-only by trigger. `AuditService.record` never raises into the business path. `AuditAction` is a set of string constants. |
| Metrics | `observability/metrics.py`, Prometheus. Tenant ids are never labels. |
| Errors | `errors.py` typed taxonomy (`code`/`http_status`/`public_message`); one error shape from the API. |
| API conventions | Routers under `/api/v1`; `require_permission(...)`; `enforce_csrf` on unsafe methods; bounded pagination; `extra="forbid"` schemas. |
| Frontend | React 19 + TypeScript strict (`noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `verbatimModuleSyntax`) + Vite + Tailwind; zod response validation in `api/schemas.ts`; `useAsync`/`AsyncBoundary`; permission-gated routes. |
| Tests | pytest + pytest-asyncio against a **real** PostgreSQL database; migrations applied by the suite; `@pytest.mark.security` marks security acceptance tests. Baseline: **637 passing**. |
| Numerical dependencies | **None.** No NumPy/SciPy/pandas/scikit-learn. Layer 3's statistical core is pure standard library. |

### 0.1 Blocking-issue check

The brief requires a stop-and-report if Layer 1–3 interfaces are incomplete or unreleased.
Neither holds:

* Layer 1 is released; Layer 2's canonical contract is complete and versioned; Layer 3's
  detection contract is complete and versioned.
* The one real integration gap is **publication**, not the contracts. Layer 3 detection
  events are not pushed downstream, and Layer 2's outbox consumer is an external sink. This
  is a *design input*, not a blocker: Layer 4 consumes both through the same bounded,
  watermark-driven, tenant-scoped read pattern Layer 3 already uses, and emits its own
  events through a Layer 4 outbox (see §6). No upstream layer is modified to make this work.

### 0.2 Dependency decision

Layer 4 adds **no new runtime dependency and no new infrastructure**. Derivation,
classification, deadline arithmetic, dependency resolution, blocker inference, follow-up
computation, and conflict detection are deterministic and dependency-free. Deadlines are
computed with `datetime` and the standard library only. No holiday calendar, working-hours
model, or SLA engine is invented; the hooks for those exist and are documented as
configuration, not fabricated (§7).

---

## 1. Scope and boundaries

Layer 4 turns verified Layer 2 canonical records and Layer 3 detection events into a
**structured, connected, continuously updated representation of organizational work**.

### 1.1 In scope

Work entities; task intelligence; commitments; deadlines and due-date intelligence;
ownership and responsibility mapping; departmental work intelligence; workflow
intelligence; dependency management; blocker and waiting-state detection; follow-up
intelligence; communication-to-work association (consuming upstream extraction results);
work timeline and activity history; work prioritization metadata; personal, managerial, and
executive views; work data-quality and conflict management; work intelligence events; and
downstream integration contracts.

### 1.2 Explicitly out of scope

Layer 4 **does not** implement:

* Long-term memory storage or consolidation (Layer 5).
* Knowledge-graph or relationship reasoning (Layer 6).
* Semantic retrieval or evidence synthesis (Layer 7).
* LLM root-cause investigation or natural-language reasoning (Layer 8).
* Decision planning or recommended-action selection (Layer 9).
* Agent orchestration (Layer 10).
* Autonomous task execution, email/WhatsApp replies, ERP writes, purchase-order placement,
  payment execution, machine control, or autonomous approvals (Layer 11).

Layer 4 may *state* that a task is overdue, a commitment is approaching, work is blocked, a
department is waiting on another, or a detection relates to existing maintenance work. It
never decides or executes the response.

Layer 4 is **not** an LLM reasoning layer. It consumes extraction results from an approved
upstream extractor through a canonical interface (§9) and performs only deterministic
handling of structured records itself.

---

## 2. Data flow

```
canonical_records (Layer 2)                detection_events (Layer 3)
        │  bounded, watermark-driven,              │  bounded, watermark-driven,
        │  tenant-scoped read                      │  tenant-scoped read
        ▼                                          ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Work Intelligence Engine (deterministic derivation)      │
   │  identify → classify → map status → assign → relate       │
   │  → deadline → dependency → blocker → follow-up → conflict │
   └───────────────────────────────────────────────────────────┘
        │                                          │
        ▼                                          ▼
   work_* tables (canonical work model)      work_timeline_events (append-only)
        │
        ▼
   work_outbox_events (transactional) ──► publisher (disabled | log | http)
        │
        ▼
   read back through the versioned /api/v1/work API and the React UI
```

Derivation is **idempotent and repeatable**: re-running over the same window upserts on
natural keys and never double-counts. It is **bounded**: a run reads at most a configured
row limit and window. It is **provenance-preserving**: every derived value records where it
came from and how confident the derivation is.

---

## 3. Module map (`humisense/layer4/`)

The brief lists twenty logical modules. They are consolidated where that produces a
simpler, safer design. The consolidation is deliberate, not a reduction in capability.

| Brief module | Implemented as | Why combined |
| --- | --- | --- |
| 1. Work Entity Management | `contract.py` + `repository.py` + `engine.py` | The entity and its persistence are one concern. |
| 2. Task Intelligence | `derivation.py` (`derive_work`) | Task identification/classification is derivation. |
| 3. Commitment Management | `derivation.py` (`CommitmentDraft`) + `repository.py` (`work_commitments`, `work_commitment_history`) | The commitment is a derived value with its own append-only history table. |
| 4. Deadline Intelligence | `deadlines.py` | Deterministic arithmetic, one home. |
| 5. Ownership / Responsibility | `ownership.py` | Ownership resolution and its unresolved states. |
| 6. Departmental Work Intelligence | `departments.py` | Department mapping + per-department aggregation. |
| 7. Workflow Intelligence | `workflows.py` | Definitions, instances, step state. |
| 8. Dependency Management | `dependencies.py` | Edge model + cycle detection. |
| 9. Blocker / Waiting-State | `blockers.py` | Blocker model + waiting derivation. |
| 10. Follow-up Intelligence | `followups.py` | Follow-up rules. |
| 11. Communication-to-Work | `extraction.py` | Canonical extraction interface + deterministic handling. |
| 12. Operational Status | `status.py` | Canonical status + source-status mapping. |
| 13. Work Timeline | `contract.py` (`WorkTimelineEvent`) + `repository.py` (`work_timeline_events`) | Append-only activity history, written by the repository. |
| 14. Prioritization Metadata | `priorities.py` | Rule-derived indicators, never human priority. |
| 15. Personal View | `views.py` (`personal`) | Read models over the same tables. |
| 16. Managerial View | `views.py` (`manager`) | Read models over the same tables. |
| 17. Executive Overview | `views.py` (`executive`) | Read models over the same tables. |
| 18. Data Quality / Conflict | `conflicts.py` | Conflict detection + resolution states. |
| 19. Event Management | `events.py` | Layer 4 outbox + event contracts. |
| 20. Downstream Contracts | `contract.py` + `events.py` | The versioned shapes later layers read. |

Supporting modules: `errors.py` (Layer 4 error taxonomy), `policy.py` (tenant-configured
derivation policy), `__init__.py`.

---

## 4. Contracts (`humisense/layer4/contract.py`)

Versioned `WORK_SCHEMA_VERSION = "4.0"`. The full field-level definition is in
[`docs/LAYER4-WORK-DOMAIN.md`](LAYER4-WORK-DOMAIN.md). The stable shapes are:

| Contract | Purpose |
| --- | --- |
| `WorkEntity` | The canonical unit of work. |
| `WorkSourceReference` | A link from a work entity to the source record(s) that describe it. |
| `WorkAssignment` | One participant and their role in the work. |
| `WorkStatus` / `WorkStatusTransition` | Canonical status and the legal transition graph. |
| `SourceStatusMapping` | Source-specific status → canonical status, preserving the original. |
| `WorkCommitment` | A promise with a party, a due date, and a verification state. |
| `WorkDependency` | A typed edge between work items or external entities. |
| `WorkBlocker` | Why work is not progressing, with evidence and a verification state. |
| `WorkFollowUp` | Something requiring follow-up, with a reason and urgency. |
| `WorkflowDefinition` / `WorkflowInstance` / `WorkflowStep` | Configurable workflow model. |
| `WorkTimelineEvent` | One append-only activity record. |
| `WorkPriority` | Deterministic prioritization metadata. |
| `WorkConflict` | A detected conflict and its resolution state. |
| `WorkExtraction` | An upstream-extracted candidate work fact, with confidence. |
| `WorkEvent` | A Layer 4 output event. |

Every contract uses `extra="forbid"`, timezone-aware timestamps, and explicit bounded
fields, matching Layers 2 and 3.

---

## 5. Storage design (migration `0004_layer4`)

All tables carry `organization_id NOT NULL` and receive `ENABLE` + `FORCE` RLS with the
same `app_current_org()` policy as Layers 1–3. Tables are grouped:

* **Core work:** `work_entities`, `work_source_references`, `work_assignments`,
  `work_status_history`, `work_priorities`.
* **Commitments & deadlines:** `work_commitments`, `work_commitment_history`.
* **Structure:** `work_dependencies`, `work_blockers`, `work_followups`,
  `workflow_definitions`, `workflow_instances`, `workflow_steps`.
* **History & quality:** `work_timeline_events`, `work_conflicts`, `work_extractions`.
* **Events:** `work_outbox_events`.

Design rules, applied consistently with Layers 2–3:

1. Normalized relational columns for stable domain fields; JSONB only for genuinely
   flexible attributes (policy bindings, workflow step definitions, extraction fields).
2. A unique constraint on the natural key of every derived table, so re-derivation is an
   upsert rather than a duplicate.
3. Indexes follow query patterns: list by tenant, filter by tenant+status, tenant+owner,
   tenant+department, tenant+due date, and order by tenant+time.
4. Append-only triggers on `work_timeline_events`, `work_status_history`,
   `work_commitment_history`, and `work_outbox_events` (the ledger tables), so history
   cannot be silently rewritten.
5. Reversible migration: `downgrade()` drops every table, every enum, and the RLS policies.

`db/rls.py` gains `LAYER4_RLS_TABLES` and `apply_layer4_rls()` / `remove_layer4_rls()`,
mirroring the Layer 3 functions exactly. This is required, not optional: the existing
security test `test_security_rls.py::test_every_tenant_table_is_either_rls_scoped_or_auth_scoped`
fails for any table that carries `organization_id` without an isolation mechanism.

---

## 6. Event architecture

### 6.1 Why Layer 4 owns its outbox

Layer 2's `outbox_events` is keyed to a canonical record by a NOT NULL foreign key, so it
cannot carry a work event. Layer 3 hit the same wall and documented it. Layer 4 therefore
introduces `work_outbox_events`: a transactional outbox whose aggregate reference is the
work entity (or, for events about commitments/dependencies/etc., the work entity that owns
the subject), written **in the same transaction** as the state change it describes.

### 6.2 Delivery semantics

Identical discipline to Layer 2:

* Durable first. An event row exists if and only if the state change committed.
* `disabled | log | http` modes; `disabled` is the default, so nothing leaves a deployment
  by accident.
* At-least-once. Consumers must be idempotent; each event carries an `idempotency_key`.
* Backoff with a bound; a failed event is marked `failed` and left for an operator, never
  silently dropped.
* No event loop: publication never writes a work entity, so a published event cannot
  trigger re-derivation.

### 6.3 Event catalogue

Incoming (read, not pushed): `NORMALIZED_RECORD_CREATED`, `NORMALIZED_RECORD_UPDATED`,
`ANALYTICS_DETECTION_CREATED` — observed as new/changed rows in `canonical_records` and
`detection_events` during a bounded incremental read.

Outgoing (Layer 4 → later layers): `WORK_ENTITY_CREATED`, `WORK_ENTITY_UPDATED`,
`WORK_ASSIGNED`, `WORK_STATUS_CHANGED`, `COMMITMENT_IDENTIFIED`, `COMMITMENT_UPDATED`,
`DEADLINE_APPROACHING`, `WORK_OVERDUE`, `DEPENDENCY_IDENTIFIED`, `WORK_BLOCKED`,
`FOLLOWUP_REQUIRED`, `WORK_COMPLETED`, `WORK_CONFLICT_DETECTED`.

Every event carries: event id, event type, tenant id, schema version, entity reference,
source reference, timestamp, correlation id, idempotency key, and provenance.

---

## 7. Deadlines and business calendars

Overdue status is computed deterministically from verified timestamps. Layer 4 does **not**
invent holidays, working hours, or SLA definitions. It distinguishes, and stores, the
source and type of every deadline: `ABSOLUTE`, `RELATIVE`, `SOURCE_PROVIDED`,
`CONTRACTUAL`, `ESTIMATED`, `REQUESTED`, `RECURRING`. A requested date is never promoted to
a confirmed promise. The configuration hooks for an organization-specific business calendar
and SLA policy exist and are documented; they are not fabricated.

---

## 8. Security and governance

Inherited from the platform and extended where necessary:

* **Tenant isolation:** RLS `ENABLE` + `FORCE` on every Layer 4 table; every repository
  method opens its own `tenant_session`, so a forgotten `WHERE organization_id` cannot
  cross a tenant.
* **Authorization:** new permissions `work:read` (visibility) and `work:manage` (authorized
  work-state changes) added to the deny-by-default matrix. `work:read` is granted to
  viewer and above; `work:manage` to operator and above. Department-level and record-level
  visibility is enforced in the read models, not only in the UI.
* **Record-level authorization:** the personal view returns only work the principal owns,
  is assigned, or is authorized to see by department/role; it never returns a work item
  merely because the principal's name appears in a conversation.
* **Untrusted source data:** emails, messages, and documents are untrusted. Layer 4 never
  executes instructions embedded in source content. Extraction results are data, never
  directives.
* **Audit:** authorized work-state changes are audited through the existing
  `AuditService`, with new `AuditAction` constants.
* **Secrets:** never in responses, logs, audit metadata, or the frontend bundle.
* **No external writes:** Layer 4 exposes no endpoint that sends anything to a supplier,
  customer, or external system.

---

## 9. Communication-to-work interface

Layer 4 does not perform LLM extraction and does not fake it. It defines the canonical
interface by which an approved upstream extractor publishes candidate work facts, and it
performs deterministic handling of what it receives:

1. A request is not an accepted commitment.
2. A proposed deadline is not a confirmed deadline.
3. A person mentioned in a message is not the owner.
4. An extracted task is not automatically assigned.
5. Conflicting dates are preserved as conflicts.
6. Ambiguous references remain unresolved.
7. External communication is untrusted data.
8. Prompt-injection text is never an instruction.

When no extractor is configured, Layer 4 still derives work from **structured** records
(the record types the tenant's derivation policy lists as derivable, `layer4/policy.py`) and
labels everything it derives with its origin and confidence.

---

## 10. Performance and reliability

* Incremental, watermark-driven reads bounded by row limit and window.
* Idempotent derivation (upsert on natural keys) safe to repeat and to backfill.
* Bounded background jobs with retry/backoff at the queue boundary (arq) and the outbox
  boundary (Layer 4 publisher).
* Indexed date, status, owner, and department queries.
* Concurrency is per tenant; no cross-tenant transaction.
* Measured, not claimed: `docs/LAYER4-RELEASE.md` records the numbers produced by the
  Layer 4 benchmark on the recorded hardware. No real-time or millisecond claim is made
  without a measurement.

---

## 11. Risks and limitations

Carried into `docs/LAYER4-RELEASE.md`, and stated here so no reader has to discover them:

1. **No ERP/CRM schema exists.** Business records are `{record_kind, identifier, text,
   party}` plus opaque `extensions`. Layer 4 derives work from the record types the
   tenant's derivation policy lists as derivable; it does not assume a purchase-order or
   work-order schema. A tenant that has not widened the derivable set gets fewer derived
   entities, not invented ones.
2. **No detection publication.** Layer 3 detections are read back, not pushed. Association
   between a detection and work happens only when a verified relationship exists
   (entity id ↔ work machine/production reference). Layer 4 never declares a root cause.
3. **No AI extraction is claimed.** The extraction interface is real; a populated extractor
   is not present in the repository.
4. **No business calendar or SLA engine.** Deadline type and source are stored; holiday and
   working-hours policy is a documented configuration hook, not an implementation.
5. **Retention is not implemented.** Work records and timeline history are preserved
   indefinitely, consistent with the platform's stated position on retention knobs.
6. **Ownership is only as good as the source.** Where a source does not state an owner, the
   ownership state is `UNRESOLVED`; Layer 4 does not guess.
