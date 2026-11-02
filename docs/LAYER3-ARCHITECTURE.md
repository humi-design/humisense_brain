# Humisense Brain — Layer 3: Analytics, Intelligence & Detection Engine

Status: architecture and implementation. Built on Layer 1 (`v0.1.0-rc.1`) and Layer 2
(`layer2-ingestion-engine`, canonical schema `2.0`).
Audience: engineering, data architecture, industrial engineering, security review, platform
operations.

## 0. Pre-implementation findings (what actually exists)

The repository was inspected before any Layer 3 code was written. The following are facts
verified in the repository, not assumptions from documentation. Where a planned capability
depends on data that does not exist, that is stated as a limitation, not invented.

| Area | Verified reality |
| --- | --- |
| Layer 1 status | Released `v0.1.0-rc.1` (merged). Not a blocker. |
| Layer 2 status | Release candidate on branch `layer2-ingestion-engine`, CI green. Not merged to `main` yet; its contract is frozen at `CANONICAL_SCHEMA_VERSION = "2.0"`. Layer 3 is built on that branch head. |
| Canonical input | `canonical_records` (migration `0002_layer2`): `organization_id`, `connection_id`, `idempotency_key`, `record_type`, `entity_type`, `event_type`, `data_family`, five timestamps, `normalized_payload` JSONB, `extensions` JSONB, `quality_code`, `schema_version`, `processing_state`. |
| Effective event time | `CanonicalRecord.effective_event_time()` = `event_timestamp or source_timestamp`. Never a processing time. |
| Industrial data shape | Historian normalizer output: `{asset_id, tag, value, unit, quality_code}` with `data_family="timeseries"`, `entity_type` = `"asset"` or `"tag"`. Units are canonicalized to SI-ish forms (`degC`, `kPa`, `mm_s`, `units_min`, …) with an auditable `unit_conversions` record. |
| Available industrial signals | Whatever the source reports as `tag`. The committed synthetic fixture and benchmark use `temperature`, `pressure`, `vibration`, `throughput` on `LINE-01..03`. These are the signals Layer 3's industrial engine is tested against. |
| Business data shape | Business normalizer output: `{record_kind, identifier, text, party}` with `data_family="business"`, `entity_type` = the record kind (`order`, `inventory`, `production`, `maintenance`, `email`, `document`, `task`, `conversation`, `record`). **Every other field is preserved opaquely in `extensions`.** |
| Business schema reality | There is **no** fixed ERP/CRM/procurement schema in the repository, and no business CSV fixture. Layer 3 must therefore treat business metrics as *tenant-configured bindings* onto extension fields, never as a hardcoded schema. |
| Generic data shape | Generic normalizer output: `{fields, structured, field_count}` with `data_family="generic"`. |
| Tenant isolation | `db/rls.py`: `ENABLE` + `FORCE` RLS, policy `organization_id = app_current_org()` reading a transaction-local GUC. `db/session.py`: `tenant_session` / `system_session`. The app role is `NOSUPERUSER NOBYPASSRLS`. |
| Authorization | `security/permissions.py` deny-by-default role matrix (viewer/operator/admin/owner), enforced in the API dependency layer and the services. |
| Queue & workers | `arq` over Redis. `workers/worker.py` `WorkerSettings` uses **class attributes** (arq reads `settings_cls.__dict__`); cron jobs drain the Layer 2 outbox each minute. |
| Audit | `audit_events`, append-only by trigger. `AuditService.record` never raises into the business path. |
| Metrics | `observability/metrics.py`, Prometheus; tenant ids are never labels. |
| Errors | `errors.py` typed taxonomy with `code` / `http_status` / `public_message`; one error shape from the API. |
| API conventions | Routers under `settings.api_prefix` (`/api/v1`); `require_permission(...)` dependency; CSRF double-submit on unsafe methods; bounded pagination; `extra="forbid"` schemas. |
| Frontend | React 19 + TypeScript strict + Vite + Tailwind; zod response validation in `api/schemas.ts`; `useAsync`/`AsyncBoundary`; permission-gated routes. |
| Numerical dependencies | **None.** No `numpy`, `scipy`, `pandas`, `scikit-learn`, or `statsmodels` in the lockfile. |
| Tests | pytest + pytest-asyncio against a **real** PostgreSQL database; migrations applied by the suite; `@pytest.mark.security` marks security acceptance tests. |

### 0.1 Blocking-issue check

The brief requires a stop-and-report if Layer 1/2 are unreleased or their contracts
incomplete. Neither holds: Layer 1 is released, and Layer 2's canonical contract is complete
and versioned. The one sequencing note is that Layer 2 is a release candidate, not yet merged
to `main`; Layer 3 therefore branches from the Layer 2 head and should merge after Layer 2.
This is not a contract gap.

### 0.2 Dependency decision

Layer 3 adds **no new runtime dependency**. The statistical core is implemented in
`humisense/layer3/stats.py` in pure Python on the standard library (`math`, `statistics`).

- Why: the brief asks for dependable, explainable, testable statistics before sophisticated
  algorithms. A ~400-line, heavily tested, dependency-free core is auditable line by line and
  adds no supply-chain surface to a security-first platform.
- Cost: pure Python is slower per operation than vectorized NumPy. At the measured data scale
  (Layer 2 benchmark: ~133 records/s sustained, windows are per-entity and bounded) this is
  not a constraint, and the benchmark in the release document measures it.
- Alternatives: NumPy/SciPy (large surface, binary wheels, GPL-compatible but heavy);
  scikit-learn (adds the whole scientific stack for one estimator). Neither is justified by a
  measured requirement.
- Isolation Forest and the other detectors are implemented in the same dependency-free style
  with deterministic seeds, so results are reproducible and reviewable.

This decision is revisited if a measured workload requires it, exactly as the Layer 2
time-series-store decision is.

## 1. Scope and boundaries

Layer 3 turns verified Layer 2 canonical records into **measurable, traceable analytical
outputs**: data-quality and readiness assessments, features, baselines, rule evaluations,
anomaly detections, trends, change points, forecasts, correlations, and standardized detection
events with a severity and a lifecycle.

Layer 3 **does not**: reason in natural language, retrieve evidence, build a context or
knowledge graph, plan decisions, orchestrate agents, or take any external action. It detects
and measures; Layers 4+ understand, remember, retrieve, reason, plan, and act. Correlation is
never presented as causation, and an anomaly is never presented as a confirmed fault.

## 2. Data flow

```
canonical_records (Layer 2)
      │  tenant-scoped read, effective_event_time ordered, bounded window
      ▼
Data Quality  →  Data Readiness  →  Feature Engineering  →  Baseline Retrieval
      ▼
Statistical & Rule Evaluation  →  Anomaly Detection  →  Trend / Change-Point
      ▼
Forecast / Correlation (where applicable)  →  Severity  →  Detection Event
      ▼
Durable persistence  →  read back through the API (downstream publication to Layers 4+ is
                        pending a detection-capable outbox; see §10)
```

Every stage is skippable when its preconditions are unmet (for example, readiness returns
`INSUFFICIENT_DATA` and no detection is produced), and every result carries its method,
version, parameters, data-quality status, and provenance references.

## 3. Contracts (Layer 3's stable, versioned interfaces)

`humisense/layer3/contract.py`, versioned `ANALYTICS_SCHEMA_VERSION = "3.0"`.

| Contract | Purpose | Key fields |
| --- | --- | --- |
| `DataQualityResult` | Suitability of a window for analysis | `status`, `issues[]` (`code`, `severity`, `field`, `detail`), `counts`, `window_start/end`, `evaluated_count` |
| `ReadinessResult` | Whether a method may run | `status` ∈ {READY, READY_WITH_WARNINGS, INSUFFICIENT_DATA, INVALID_DATA, STALE_DATA}, `reasons[]`, `sample_count`, `min_required` |
| `FeatureValue` | One reproducible feature observation | `name`, `version`, `value`, `unit`, `at`, `entity_id`, `method`, `window_seconds`, `quality_status`, `sources[]` |
| `BaselineVersion` | A versioned normal-behaviour model | `baseline_id`, `version`, `type`, `window`, `mean`, `median`, `stddev`, `p10/p25/p75/p90`, `min/max`, `unit`, `sample_count`, `quality_status`, `dataset_ref` |
| `RuleDefinition` | A tenant-configurable rule | `rule_id`, `name`, `scope`, `metric`, `condition`, `threshold`, `window_seconds`, `severity`, `enabled`, `version` |
| `DetectionEvent` | The standardized output | the full field list from the brief (see §7) |
| `AnalyticsRun` | One execution's record | `run_id`, `job_type`, `entity_id`, `metric`, `status`, `counters`, `started_at`, `finished_at`, `error_code` |

`DetectionEvent` field names follow the brief exactly (`detection_id`, `tenant_id`, `domain`,
`source_system`, `connection_id`, `entity_id`, `entity_type`, `metric`, `detection_category`,
`detection_type`, `severity`, `observed_value`, `expected_value`, `deviation`, `unit`,
`observation_window_start/end`, `detected_at`, `algorithm_name`, `algorithm_version`,
`model_id`, `model_version`, `baseline_id`, `baseline_version`, `feature_versions`,
`anomaly_score`, `confidence`, `data_quality_status`, `evidence_references`, `explanation`,
`limitations`, `correlation_id`, `schema_version`).

## 4. Storage design

Migration `0003_layer3`, tables under the same RLS treatment as Layers 1–2. Design rules:
every tenant-owned row carries `organization_id`; JSONB only where the shape is genuinely
variable (rule condition, detection evidence, quality issues); provenance and versions are
columns, not buried in JSON; writes are idempotent on a natural key; indexes follow the query
patterns (tenant, tenant+entity, tenant+metric+time).

| Table | Purpose | Idempotency key |
| --- | --- | --- |
| `analytics_jobs` | One scheduled/triggered execution | `(organization_id, job_key)` |
| `analytics_execution_logs` | Append-only per-run stage log | append-only |
| `data_quality_results` | Quality assessment per entity/metric/window | `(organization_id, entity_id, metric, window_end)` |
| `data_readiness_results` | Readiness per method/entity/metric | `(organization_id, entity_id, metric, method, window_end)` |
| `feature_definitions` | Registered, versioned feature definitions | `(organization_id, name, version)` |
| `feature_values` | Computed feature observations | `(organization_id, entity_id, metric, name, version, at)` |
| `baseline_definitions` | A baseline's identity and configuration | `(organization_id, entity_id, metric, baseline_type)` |
| `baseline_versions` | Immutable baseline versions | `(organization_id, baseline_id, version)` |
| `analytics_rules` | Rule identity | `(organization_id, rule_id)` |
| `analytics_rule_versions` | Immutable rule versions | `(organization_id, rule_id, version)` |
| `anomaly_detections` | Raw detector output | `(organization_id, entity_id, metric, algorithm, window_end, baseline_version)` |
| `trend_results` | Trend analysis output | `(organization_id, entity_id, metric, window_end)` |
| `change_point_results` | Change-point output | `(organization_id, entity_id, metric, detected_at)` |
| `forecast_results` | Forecast output | `(organization_id, entity_id, metric, generated_at, horizon)` |
| `correlation_results` | Correlation output | `(organization_id, entity_a, metric_a, entity_b, metric_b, window_end)` |
| `model_registry` | Model identity and versions | `(organization_id, model_key, version)` |
| `model_evaluations` | Evaluation per model version | `(organization_id, model_id, dataset_ref)` |
| `detection_events` | The standardized detection | `(organization_id, detection_id)` |
| `detection_event_history` | Append-only lifecycle transitions | append-only |

Retention: detection events and their history are retained indefinitely by default (they are
the analytical record of the platform). Execution logs and raw feature values are the
candidates for a future retention policy; none is implemented, and no knob implies one.

## 5. Execution architecture

`humisense/layer3/engine.py` is the single orchestrator. It supports scheduled (arq cron),
event-triggered (a canonical record arrives), and manual (API) execution; incremental
processing by watermark (`feature_values.at` per entity/metric); bounded windows; per-entity
and per-tenant scoping; idempotent writes; checkpoint recovery via the job row; and retry with
backoff using the existing Layer 1 retry policy.

The scheduled path is `analytics_sweep_task` in `workers/worker.py`, which calls
`AnalyticsEngine.run_sweep`. It is a no-op unless `layer3_sweep_enabled` is set, so detection
does not run on a timer a deployment did not ask for. The sweep enumerates tenants, then runs
each one inside its own tenant-scoped sessions; one tenant's failure is logged and does not
stop the sweep.

Nothing loads a whole historian into memory: a run reads a bounded window for one entity and
one metric at a time, in tenant-scoped transactions.

## 6. Module boundaries (`humisense/layer3/`)

| Module | Responsibility |
| --- | --- |
| `stats.py` | Pure statistical primitives (mean, median, stddev, quantiles, IQR, z-score, robust z-score, EWMA, CUSUM, linear regression, Pearson/Spearman) |
| `quality.py` | Data-quality intelligence (missing, stale, out-of-order, irregular, invalid, non-finite, schema drift, unit inconsistency, frequency change, insufficient history) |
| `readiness.py` | Precondition evaluation per method |
| `features.py` | Versioned, reproducible feature engineering with leakage guards |
| `baselines.py` | Baseline construction, versioning, drift guard |
| `rules.py` | Safe rule evaluation (no `eval`, no arbitrary SQL) |
| `detectors.py` | Statistical, baseline-based, and ML detectors |
| `trend.py` | Trend and change-point analysis |
| `forecast.py` | Interpretable forecasting with chronological evaluation |
| `correlation.py` | Signal correlation with alignment and a no-causation statement |
| `severity.py` | Severity evaluation from deviation, duration, persistence, rarity, spread, quality |
| `industrial.py` | Industrial signal extraction and multivariate analysis over `timeseries` records |
| `business.py` | Business-domain analytics over tenant-configured metric bindings on `business` records |
| `models.py` | Model registry, evaluation, activation/rollback |
| `events.py` | Detection-event creation, dedup grouping, cooldown, hysteresis, recovery |
| `repository.py` | Tenant-scoped persistence and window reads |
| `engine.py` | Orchestration of the flow in §2 |

## 7. Detection-event lifecycle

`OPEN → ACKNOWLEDGED → INVESTIGATING → RESOLVED`, plus `SUPPRESSED` and `RECOVERED`.
Transitions are recorded append-only in `detection_event_history`. Deduplication groups
detections by `(entity, metric, algorithm, window)`; a cooldown window and hysteresis prevent
flapping; recovery is detected when the signal returns inside the baseline band. A previously
seen anomaly is never permanently suppressed — suppression is time-bounded and audited.

## 8. Security and multi-tenancy

Layer 3 reuses and preserves every Layer 1/2 control: RLS on all new tables, deny-by-default
permissions (new: `analytics:read`, `analytics:manage`), tenant-scoped sessions for every read
and write, audit events for sensitive operations, and no source payloads in detection events
(references only). Rule conditions are a bounded, validated expression tree — never `eval`,
`exec`, or SQL. Model artifacts are never deserialized from arbitrary files; the registry
stores metadata and a reference, and inference runs only in-process from code. A tenant can
never read another tenant's features, baselines, rules, models, forecasts, or detections; this
is asserted by RLS tests and cross-tenant API tests.

## 9. Performance targets

Measured, not claimed, in `docs/LAYER3-RELEASE.md`. Targets to evaluate against: a bounded
analytical window (default ≤ 10 000 samples/entity/metric) analyzes in well under a second; a
scheduled sweep over a multi-entity tenant completes within the cron interval; memory is a
function of window size, not table size. No real-time or millisecond claim is made without a
benchmark on the recorded hardware.

## 10. Risks and limitations (carried into the release document)

- The business normalizer preserves arbitrary extension fields; business analytics is only as
  good as the tenant's metric bindings. No ERP schema is assumed.
- No real (non-synthetic) historian data is present; industrial validation uses the committed
  synthetic fixture and is labelled as such.
- Isolation Forest and correlation are offered but not enabled by default; they are enabled per
  tenant/entity only where the data characteristics justify them.
- Retention for feature values and execution logs is not implemented.
- The outbox row is keyed to a canonical record (a NOT NULL foreign key to
  `canonical_records`), so detections cannot be published through it as-is. Publishing
  detection events to Layers 4+ requires either a nullable aggregate reference or a
  separate detection outbox; until then detections are durable in `detection_events` and
  read back through the API rather than pushed downstream.
- Layer 2 is a release candidate; Layer 3 depends on its branch until it merges.
