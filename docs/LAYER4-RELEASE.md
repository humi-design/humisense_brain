# Humisense Brain — Layer 4 Release Record

Status: implementation complete; measured. This document records what was built, what was
measured, what passed, and what remains open. It is a point-in-time record, like
`docs/LAYER2-RELEASE.md`.

Companion documents: [`LAYER4-ARCHITECTURE.md`](LAYER4-ARCHITECTURE.md) (architecture),
[`LAYER4-WORK-DOMAIN.md`](LAYER4-WORK-DOMAIN.md) (domain model).

---

## 1. What is implemented

Layer 4 derives and maintains a canonical work model from verified Layer 2 canonical
records and Layer 3 detection events. It is a work-understanding layer: it states what
work exists, who is responsible, what is committed, what is overdue, what is blocked, and
what requires follow-up. It does not remember, connect, retrieve evidence, reason, plan, or
act.

| Capability | Where | State |
| --- | --- | --- |
| Canonical work entity + source references | `layer4/contract.py`, `repository.py`, `derivation.py` | Implemented |
| Status model and source-status mapping | `layer4/status.py` | Implemented |
| Commitments (with history) | `layer4/derivation.py`, `work_commitments` | Implemented |
| Deadline intelligence (typed, timezone-aware) | `layer4/deadlines.py` | Implemented |
| Ownership and responsibility | `layer4/ownership.py` | Implemented |
| Departmental work intelligence | `layer4/departments.py` | Implemented |
| Workflow definitions and observed state | `layer4/workflows.py` | Implemented |
| Dependency model + cycle detection | `layer4/dependencies.py` | Implemented |
| Blockers and waiting states | `layer4/blockers.py` | Implemented |
| Follow-up rules | `layer4/followups.py` | Implemented |
| Communication-to-work interface | `layer4/extraction.py` | Interface implemented; no extractor configured |
| Work timeline (append-only) | `contract.py`, `work_timeline_events` | Implemented |
| Prioritization metadata | `layer4/priorities.py` | Implemented |
| Personal / department / executive views | `layer4/views.py`, `services/work.py` | Implemented |
| Data-quality and conflict management | `layer4/conflicts.py` | Implemented |
| Work intelligence events (transactional outbox) | `layer4/events.py`, `work_outbox_events` | Implemented |
| Versioned API | `api/routers/work.py` | Implemented |
| React + TypeScript experience | `frontend/src/pages/*`, `components/WorkItemTable.tsx` | Implemented |

Not implemented, by design, and documented as such: long-term memory, knowledge-graph
reasoning, semantic retrieval, LLM investigation, planning, agent orchestration, and any
external action (email, message, ERP write, order placement, payment). Those belong to
Layers 5–11.

---

## 2. Regression and suite results

The full backend suite was run against a real PostgreSQL database after the Layer 4 work:

```
715 passed, 1 warning in 184.52s
```

Baseline before Layer 4 was **637 passing**. Layer 4 adds **78 tests** across
`tests/layer4/test_unit_domain.py`, `tests/layer4/test_engine.py`, and
`tests/layer4/test_security_and_api.py`. No pre-existing test was deleted or weakened.

Quality gates:

| Gate | Result |
| --- | --- |
| `uv run pytest` | 715 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 155 files already formatted |
| `uv run mypy src` | Success: no issues found in 116 source files |
| `uv run bandit -c pyproject.toml -r src` | 0 high, 0 medium; 4 low (all pre-existing Layer 3 `assert` uses) |
| `npm run typecheck` | Clean |
| `npm run lint` | Clean |
| `npm test` | 15 passed (2 files) |
| `npm run build` | Built |

---

## 3. Performance measurements

Measured with the Layer 4 benchmark, which ingests real business records through the real
Layer 2 pipeline and then runs the real derivation engine and read models:

```
uv run python -m humisense.devtools.cli work-benchmark --records 5000
```

The command needs a database and a secrets master key, exactly as the API does:

```
DATABASE_URL=postgresql+asyncpg://humisense_app:...@localhost:5432/humisense_test \
SECRETS_MASTER_KEY=<base64-of-32-random-bytes> \
  uv run python -m humisense.devtools.cli work-benchmark --records 5000
```

Fixture: 5,000 business records (`task`, `purchase_order`, `maintenance_event`), each with a
title, status, priority, due date, owner party, and machine reference.

Environment: AMD EPYC 9B14, 4 vCPU, 16 GB RAM, PostgreSQL 16.4, Python 3.13.

| Metric | Value |
| --- | --- |
| Records ingested | 5,000 accepted, 0 quarantined, 0 failed |
| Work entities derived | 5,000 created, 0 updated, 0 skips |
| Ingest time | 44.2 s (≈113 records/s, the Layer 2 path) |
| Derivation time | 191.6 s (≈26 work entities/s) |
| `my_work` view latency | 14 ms |
| Work list (50 rows) latency | 5 ms |
| Peak RSS | 136 MB |

These are single-run numbers on a shared 4-vCPU host, not a tuned or distributed
measurement. They are recorded to establish order of magnitude, not to claim a service
level. The derivation rate is dominated by per-record upserts and outbox inserts, not by
read cost; the read models the UI uses are in the millisecond range.

---

## 4. Security results

| Control | Evidence |
| --- | --- |
| Every Layer 4 table carries `organization_id NOT NULL`, `ENABLE` + `FORCE` RLS | `db/rls.py::LAYER4_RLS_TABLES`; `tests/layer4/test_security_and_api.py::test_every_layer4_table_has_rls_enabled_and_forced`; `tests/test_security_rls.py` |
| Every repository call opens its own `tenant_session` | `layer4/repository.py` (structural, not per-query filtering) |
| Cross-tenant read is impossible | `tests/layer4/test_security_and_api.py` cross-tenant cases |
| New permissions are deny-by-default | `security/permissions.py`: `work:read` (viewer+), `work:manage` (operator+) |
| Record-level visibility | `layer4/views.py`: personal view is owner/assignee scoped; a name in a message is never sufficient |
| Unauthorized status change / assignment is refused | `tests/layer4/test_security_and_api.py` authorization cases |
| Authorized work-state changes are audited | `services/work.py` via `AuditService`; `AuditAction.WORK_*` |
| External communication is untrusted and never executed | `layer4/extraction.py`; no code path interprets source content as an instruction |
| No external write surface | the router has no endpoint that sends anything to a supplier, customer, or system |

---

## 5. Reliability results

| Property | Mechanism | Test |
| --- | --- | --- |
| Idempotent re-derivation | upsert on natural keys | `tests/layer4/test_engine.py::test_rerun_is_an_idempotent_upsert` |
| Durable-first events | outbox row written in the same transaction as the state change | `tests/layer4/test_engine.py` event cases |
| At-least-once delivery, idempotent consumers | `idempotency_key` on every event; `on_conflict_do_nothing` | `layer4/events.py` |
| No event loop | publication never writes a work entity | `layer4/events.py` |
| Per-record isolation | one bad record is a `skip`, not a run failure | `tests/layer4/test_engine.py::test_a_derivation_failure_is_a_skip_and_the_run_is_partial` |
| Partial runs are labelled | a run with any skip finishes `PARTIAL` | `tests/layer4/test_engine.py::test_a_derivation_failure_is_a_skip_and_the_run_is_partial` |
| Bounded work | row limit + window on every read | `layer4/repository.py`, engine |
| History is immutable | append-only triggers on the ledger tables | migration `0004_layer4` |

---

## 6. Acceptance criteria

| # | Criterion | State |
| --- | --- | --- |
| 1 | Consumes verified Layer 2 records | Yes — bounded, watermark-driven read |
| 2 | Consumes Layer 3 detection events | Yes — associated by verified reference only |
| 3 | Stable canonical work model | Yes — `WORK_SCHEMA_VERSION = "4.0"` |
| 4 | Preserves source provenance | Yes — every derived row carries a source reference |
| 5 | Identifies and maintains work entities | Yes |
| 6 | Supports explicit task ownership | Yes; unresolved when the source is silent |
| 7 | Supports departmental responsibility | Yes |
| 8 | Tracks commitments | Yes, with history |
| 9 | Tracks deadlines | Yes, with type and source |
| 10 | Identifies overdue work | Yes, deterministic |
| 11 | Models dependencies | Yes, explicit and inferred (labelled) |
| 12 | Tracks blockers | Yes, `UNKNOWN` when unproven |
| 13 | Identifies follow-ups by documented rules | Yes — `layer4/followups.py` |
| 14 | Supports configurable workflows | Yes |
| 15 | Preserves work timelines | Yes, append-only |
| 16 | Handles duplicate and conflicting records | Yes — upsert + conflict records |
| 17 | Maintains tenant and record-level authorization | Yes |
| 18 | Exposes stable, versioned APIs | Yes — `/api/v1/work/*` |
| 19 | Integrates with React + TypeScript | Yes |
| 20 | Processes updates reliably | Yes — idempotent, bounded, durable-first |
| 21 | Comprehensive automated tests | Yes — 78 Layer 4 tests |
| 22 | Passes Layer 1–3 regression suites | Yes — 715 passed |
| 23 | Documented limitations and performance | Yes — this document |

---

## 7. Known limitations and accepted risks

These are real and are carried forward deliberately.

1. **No ERP/CRM schema exists.** Business records are `{record_kind, identifier, text,
   party}` plus opaque `extensions`. Layer 4 derives work from record types the policy
   lists as derivable (`layer4/policy.py`). A tenant that has not widened the derivable set
   gets fewer derived entities, not invented ones. A record type outside the derivable set
   (for example a generic CSV row) derives no work — this is the policy working as intended,
   not a defect. The benchmark uses three derivable kinds, so it derives 5,000 of 5,000.

2. **No detection publication.** Layer 3 detections are read back, not pushed. Association
   happens only when the work's `machine_ref` matches the detection's `entity_id`. The
   benchmark reports `detections_associated: 0` because it seeds no detections.

3. **No AI extraction is claimed.** The communication-to-work interface is real and
   deterministic. No extractor is present in the repository, so no extraction is performed
   and none is faked.

4. **No business calendar or SLA engine.** Deadline type and source are stored; holiday and
   working-hours policy is a documented configuration hook, not an implementation. Overdue
   is computed from verified timestamps without a calendar.

5. **Retention is not implemented.** Work records and timeline history are preserved
   indefinitely, consistent with the platform's position that a retention knob without a
   purge job is worse than no knob.

6. **Ownership is only as good as the source.** Where a source does not state an owner, the
   ownership state is `UNRESOLVED`. Layer 4 does not guess, and the UI shows `unresolved`.

7. **Derivation throughput is modest.** ≈26 work entities/s single-threaded on a 4-vCPU
   host. A tenant with millions of historical records needs a backfill plan (bounded
   windows, run repeatedly) rather than one unbounded run. The engine is bounded and
   idempotent, so repeated windows are safe; the schedule is not yet automated by default
   (`layer4_sweep_enabled` is `false`).

8. **`layer4_sweep_enabled` defaults to `false`.** A deployment must opt in to scheduled
   derivation once it has sized its window. The API run endpoint works regardless.

---

## 8. Readiness assessment

Distinguishing the levels the brief asks for, honestly:

| Level | Assessment |
| --- | --- |
| Local development readiness | **Ready.** Suite green, gates green, benchmark reproducible. |
| Controlled pilot readiness | **Ready.** Tenant isolation, authorization, and audit are tested; derivation is bounded and idempotent. |
| Layer 5 integration readiness | **Ready with the limitations above.** The canonical work model, source-reference contract, event contract, and read APIs are versioned and stable. |
| Production readiness | **Not claimed.** No scheduled derivation by default, no backfill plan exercised at scale, no business calendar. |
| Multi-tenant enterprise readiness | **Not claimed.** Isolation is in place and tested, but scale, backfill, and operational runbooks are not demonstrated. |

---

## 9. Release decision

**APPROVED WITH EXPLICIT ACCEPTED RISKS**

The canonical work model, data contracts, event interfaces, security controls, and
critical tests are ready, and Layer 4 is functional and safe for controlled downstream
integration. The documented limitations — no configured extractor, no business calendar,
no retention, unexercised backfill at scale, and derivation disabled on a timer by default
— are non-critical for a controlled integration and are accepted explicitly.

Production and multi-tenant enterprise readiness are **not** claimed.
