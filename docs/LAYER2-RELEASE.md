# Humisense Brain — Layer 2 Release Notes and Audit

Status: Layer 2 (Data Ingestion & Normalization Engine) prepared for release, built on
Layer 1 `v0.1.0-rc.1`.
Audience: engineering, security review, platform operations, data architecture.

This document is the release evidence for Layer 2. Every number below was produced by
running the released code against a real PostgreSQL database and, where noted, a real HTTP
server. Nothing here is inferred from the design document; the architecture is described in
`docs/LAYER2-ARCHITECTURE.md` and this document records what was measured.

## 1. What shipped

Layer 2 adds the `humisense/layer2/` package and a small number of additive changes to
Layer 1:

| Area | Change |
| --- | --- |
| Contract | `layer2/contract.py` — versioned canonical record (`CANONICAL_SCHEMA_VERSION = "2.0"`). |
| Receiver | `layer2/receiver.py` — authenticated machine endpoint, per-record isolation, byte and count bounds. |
| Pipeline | `layer2/pipeline.py` — validation, preservation, normalization, dedup, canonical storage. |
| Storage | `db/models.py` + migration `0002_layer2` — six new tenant-owned tables, all under RLS. |
| Outbox | `layer2/outbox.py` — transactional outbox publisher (`disabled` / `log` / `http`). |
| Replay | `layer2/replay.py` — authorized, audited replay of quarantined records. |
| API | `api/routers/ingestion.py` — batch receive (machine), quarantine list and replay (session). |
| Worker | `workers/worker.py` — `publish_outbox_task` cron, tenant scope re-established per job. |
| Permissions | `security/permissions.py` — `ingestion:read` (viewer+), `ingestion:replay` (admin+). |
| Tooling | `devtools/benchmark.py` — reproducible ingestion benchmark; `devtools/cli.py` gains `benchmark`. |

Layer 1 is modified in exactly two places, both additive: a sink mode so a single-node
deployment can hand off in-process, and the new permission entries. No Layer 1 behaviour
was removed or relaxed.

## 2. Verification evidence (reproduced, not assumed)

Run from `backend/` on the revision being released.

| Check | Command | Result |
| --- | --- | --- |
| Tests | `uv run pytest` | **510 passed**, 1 warning (Alembic `path_separator` deprecation) |
| Lint | `uv run ruff check .` | clean |
| Format | `uv run ruff format --check .` | 103 files already formatted |
| Types | `uv run mypy src` | clean, 77 source files |
| SAST | `uv run bandit -c pyproject.toml -r src` | 0 issues (Low/Medium/High all 0) |
| Dependencies | `uv run pip-audit` | no known vulnerabilities |
| Frontend types | `npm run typecheck` | clean |
| Frontend lint | `npm run lint` | clean |
| Frontend tests | `npm test` | 8 passed |
| Migration graph | `uv run alembic upgrade head` | applies `0001_initial` then `0002_layer2` |

The single warning is emitted by Alembic's own configuration parser and is not a defect in
this repository.

## 3. End-to-end verification against a live server

The API was started with `uvicorn humisense.main:app` against a migrated database, the
receiver enabled, and the flows below were exercised with `curl` and a signed-in session.
These results are from the live process, not the test client.

| Scenario | Request | Result |
| --- | --- | --- |
| Health | `GET /api/v1/health` | `200` |
| Receiver with no token | `POST /api/v1/ingestion/batches` | `401` |
| Receiver with wrong token | same, `Bearer wrong` | `401` |
| Receiver with a valid body but no bearer token, authenticated session | same | `401` (the machine route never accepts a session) |
| Receiver with the right token | same | `202`, `accepted: 1` |
| Redelivery of a new `batch_id` with the same record | same record, new batch | `202`, `duplicates: 1`, one canonical row |
| Retransmit of the **same** `batch_id` | identical request | `202`, `accepted: 0`, `duplicates: 1` |
| Cross-tenant connection id | record naming a connection in another tenant | `422` `validation_failed` |
| Oversized payload | ~2 MB payload | `413` (transport bound) |
| Invalid record (missing `machine_id`) | record that fails validation | `202`, `quarantined: 1` |
| Quarantine list, anonymous | `GET /api/v1/ingestion/quarantine` | `401` |
| Quarantine list, viewer session | same | `200`, lists the open record |
| Replay without CSRF | `POST .../quarantine/{id}/replay` | `403` |
| Replay with CSRF, owner session | same | `200`, `replayed: true` |
| Replay, viewer session | same | `403` (needs `ingestion:replay`) |

Audit rows written during that session (queried as superuser, which bypasses RLS):

```
ingestion.batch_received|success|5
ingestion.batch_rejected|failure|1
quarantine.replayed|success|1
connection.created|success|1
```

Database-level guarantees re-checked against the live database:

| Guarantee | Check | Result |
| --- | --- | --- |
| RLS enabled and forced | `pg_class.relrowsecurity` / `relforcerowsecurity` for the six Layer 2 tables | `t` / `t` for all six |
| RLS actually filters | `SELECT count(*) FROM raw_records` as the app role with no tenant GUC | `0` (superuser sees `3`) |
| Raw payloads immutable | `UPDATE raw_records ...` | refused: `raw_records is append-only; UPDATE is not permitted` |
| Processing ledger append-only | `DELETE FROM record_processing_events` | refused by trigger |
| Audit append-only | `DELETE FROM audit_events` | refused by trigger |

Publication was then driven with `LAYER2_PUBLISH_MODE=log`. The publisher reported
`published: 2 failed: 0 skipped: 0`; afterwards the canonical rows and their outbox events
were both in state `published`, and the processing ledger recorded the `published`
transition. Publication is only claimed after the sink confirms.

## 4. Benchmark

Reproduced with `uv run python -m humisense.devtools.cli benchmark --records N --batch-size B`
against PostgreSQL 16 on the release machine.

**Hardware:** 4 vCPU (AMD EPYC 9B14), 15.6 GiB RAM, Linux 6.8 x86_64, Python 3.13.

| Records | Batch size | Batches | Accepted | Quarantined | Failed | Elapsed (s) | Records/s | Peak RSS (MB) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2,000 | 250 | 8 | 2,000 | 0 | 0 | 14.7 | 135.9 | 92.8 |
| 10,000 | 500 | 20 | 10,000 | 0 | 0 | 74.3 | 134.6 | 97.6 |
| 50,000 | 1,000 | 50 | 50,000 | 0 | 0 | 375.8 | 133.0 | 120.3 |

Redelivery (the idempotency path) was measured by re-running the first batch of each run:
250 duplicates of 250 in 1.5 s, 500 of 500 in 2.9 s, and 1,000 of 1,000 in 5.9 s — every
record recognized as a duplicate and none stored twice.

**Reading.** Throughput is flat at ~133–136 records/s across a 25× range of dataset size,
so ingestion is linear in the record count rather than quadratic, and there is no
degradation as the table grows. Peak RSS grows slowly and sub-linearly — 92.8 MB → 120.3 MB
for 25× the records — which is the bounded-chunk design holding: memory is a function of
batch size, not of total volume. The remaining cost is dominated by per-record normalization
and insert work, which is the intended, measured baseline.

**Decision on a separate time-series store.** Not adopted. The brief required a benchmark
before adopting one; at ~133 records/s sustained and linear scaling, PostgreSQL with the
JSONB + typed-column design carries the current workload, and the added operational surface
of a second store is not justified. The benchmark is committed so the decision can be
revisited when the workload changes, rather than re-argued from memory.

## 5. Defects found and fixed during this release pass

This pass looked specifically for the class of defect found in Layer 1 F6 — a configuration
knob that is defined but never read, so an operator can set it and see no effect.

### D1 — `layer2_max_batch_bytes` was defined but not enforced (Medium) — fixed

The setting existed with a default of 8 MB, but the receiver only enforced a *record count*
limit (`MAX_RECORDS_PER_BATCH = 200`). A caller could therefore send 200 records of 1 MB
each and the "batch size" knob did nothing. Worse, the default (8 MB) was above
`max_request_body_bytes` (1 MB), so the configured bound was unreachable: the transport
would reject the request before the receiver's check could ever run.

Fixed: the receiver now sums the serialized payload and metadata of every record and
refuses the batch when it exceeds `layer2_max_batch_bytes`; the default is lowered to 1 MB
so it sits at or below the transport limit; and `require_production_ready()` refuses a
configuration where `layer2_max_batch_bytes` exceeds `max_request_body_bytes`, so the dead
configuration cannot be shipped again. Covered by
`test_ingestion_receiver_refuses_an_oversized_batch` and
`test_layer2_batch_bound_must_be_within_the_transport_limit`.

### D2 — Speculative knobs `layer2_pipeline_max_attempts` and `layer2_retention_raw_days` (Low) — fixed

Both were present in `.env.example` and `Settings` but read by nothing. `layer2_retention_raw_days`
in particular is dangerous: it *looks* like a retention control, so an operator could set it
believing raw data was being purged, when no purge exists. Both were removed rather than
wired up, because neither is implemented: adding a retention job is a Layer 2 feature
decision with its own design, and a knob that promises it without delivering it is worse
than no knob.

### D3 — Redelivering the same `batch_id` double-counted (Low) — fixed

When a producer did not observe the `202` (a dropped connection or a timeout) and
retransmitted the identical `batch_id`, the pipeline seeded the response counters from the
stored batch row and *then* added this delivery's per-record outcomes on top. A single
record was reported as both `accepted: 1` and `duplicates: 1`. The stored data was always
correct — one raw row, one canonical row — but the response an operator or producer acts on
was wrong, and it made the batch counters non-additive.

Fixed: the counters now describe what *this* delivery did. A retransmit accepts nothing new,
so every record reports as a duplicate; the durable batch row keeps the counters of the
first processing and is not rewritten by a retransmit. Covered by
`test_redelivering_the_same_batch_id_does_not_double_count`.

## 6. Verified strengths

These were re-confirmed during this pass, not merely carried over from the design:

- **Tenant isolation is enforced at the database, not the application.** All six Layer 2
  tables are `ENABLE` + `FORCE` row-level security; a query with no tenant GUC returns
  zero rows. The receiver never trusts the tenant in the request body — the named connection
  is looked up inside a tenant-scoped session, and a batch naming a foreign connection is
  refused (`422`).
- **The receiver fails closed.** With no configured ingest token every request is refused
  (`401`), and the machine route is registered only when `layer2_ingest_enabled` is true, so
  a deployment that does not accept machine writes has no such route at all rather than one
  that exists and refuses.
- **Append-only is a database guarantee.** `raw_records`, `record_processing_events`, and
  `audit_events` reject mutation with a trigger, verified by attempting the mutation.
- **Publication is durable-first.** A canonical record and its outbox event are written in
  the same transaction; the publisher moves `pending → published` only after the sink
  confirms. Verified end to end with the `log` sink.
- **Authorization is deny-by-default and least-privilege.** A viewer can read quarantine but
  cannot replay (`403`) and cannot post to the machine route; replay is admin-gated and
  CSRF-protected.
- **Sensitive operations are audited.** Accepted and rejected batches and every replay
  write audit rows, verified in the live database.

## 7. Non-claims and residual risk

- **Delivery is at-least-once, not exactly-once.** Duplicates are detected and reported, and
  deduplication is idempotent, but the system does not claim single delivery. Consumers must
  be idempotent on the canonical record identity.
- **Retention is not implemented.** Raw records are preserved indefinitely. There is no
  purge job and, after D2, no configuration that implies one. Retention is a deliberate
  future decision with its own design and a data-lifecycle review.
- **`layer2_ingest_token` bootstrap is a plain environment value.** The `*_secret_ref`
  variants resolve through the secrets provider; the inline tokens are for single-node and
  test use. A split production deployment should use the secret reference.
- **No frontend surface yet.** Layer 2 has no operator UI; the quarantine endpoints are
  reachable by API only. This is intentional for this milestone.
- **`gitleaks` was not available on the release host.** The history scan recorded for Layer 1
  was not re-run here. Bandit and `pip-audit` were run and are clean; the secret scan remains
  a CI gate.
- **The benchmark is single-process and synthetic.** It measures pipeline throughput with the
  committed synthetic historian pattern, not concurrent multi-tenant load, and it does not
  include the publisher or worker in the timing.

## 8. Release decision

Layer 2 is approved for release on the evidence above: the full suite passes, static and
dependency analysis are clean, the pipeline was verified end to end against a live server
through to downstream publication, tenant isolation and append-only guarantees were
re-verified at the database, and the configuration-knob defects of the Layer 1 F6 class were
found, fixed, and covered by tests. The residual items in section 7 are explicit non-claims,
not open defects.

The recommended next steps are the same as Layer 1's: address the supply-chain tooling and
frontend-coverage items, and perform a third-party penetration test before onboarding
multi-tenant enterprise data.
