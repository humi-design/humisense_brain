# Independent CTO Audit — Layer 1: Connector & Sensory Platform

Security, architecture, reliability, and production-readiness review.

- **Reviewer role**: independent CTO / principal software architect.
- **Method**: independent verification. Every claim below was reproduced from the running
  system or the source, not accepted from the documentation. Where documentation and code
  disagreed, the code was treated as authoritative and the documentation is listed as a
  defect.
- **Scope**: the whole repository at the audited revision — backend, frontend, docs, ops,
  CI. Layer 1 only (capture, envelope, transport, tenant isolation, secrets, egress,
  audit). Layer 2 and later are out of scope by design.

## 1. Verdict

Layer 1 is a **strong, well-above-average internal engineering baseline**. The security
model is real and enforced in depth, not decorative: tenant isolation is backed by
PostgreSQL Row-Level Security with `FORCE`, authorization is deny-by-default in a shared
service layer, secrets are encrypted with AEAD and bound to tenant and name, the egress
policy is deny-by-default, and the audit trail is append-only at the database level. These
were all verified against a live stack, not merely observed in tests.

The gaps that exist are **honesty and finishing gaps, not architecture gaps**: the
documentation claimed test files, tooling, and controls that did not exist; a performance
defect made a full CSV sync quadratic in the row count; and the documented quick start did
not work from the documented directory. All three are now fixed.

**Recommendation**: proceed as a production-readiness candidate for a controlled,
single-tenant pilot after the P1 items in section 7 are closed. Do **not** onboard
multi-tenant enterprise data until F5 (egress DNS rebinding) and the deployment-hardening
items are addressed, and a third-party penetration test has been performed.

## 2. Evidence gathered (reproduced, not assumed)

The environment was brought up exactly as CI does: Docker, PostgreSQL 16, Redis 7, the
bootstrap script creating the application and test databases, and the Alembic migration
applied. Findings were then reproduced against the live system.

| Area | Verification | Result |
| --- | --- | --- |
| Test suite | `uv run pytest` against real Postgres/Redis | 417 passed |
| Lint / format | `ruff check`, `ruff format --check` | clean, 77 files |
| Types | `mypy src` | clean, 58 files |
| SAST | `bandit -c pyproject.toml -r src` | 0 issues |
| Dependencies | `pip-audit` | no known vulnerabilities |
| Frontend | `tsc -b`, `eslint`, `vitest`, `vite build` | all green, 8 tests |
| Secret scan | `gitleaks` over history | no leaks |
| Tenant isolation | live cross-tenant requests | 404 on foreign objects, empty lists, no existence disclosure |
| CSRF | live write without / with wrong token | 403 |
| Privilege | viewer role writes / reads | 403 / 200 |
| RLS | DB query with no tenant GUC | 0 rows; enabled + forced on every tenant table |
| Audit | `UPDATE` / `DELETE` on `audit_events` | rejected by trigger |
| Secrets | stored value inspection | ciphertext only, never plaintext |
| Sync E2E | real HTTP API, 500 records | captured, persisted, delivered |
| Idempotency | re-run identical sync | 500 → 500, 0 new records |
| Streaming | 300k rows / 600 pages | bounded memory (peak RSS delta ~2 MB) |
| Performance | full 300k-row sync | 72.73 s before fix, 2.05 s after (F1) |

## 3. Findings summary

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| F1 | High | `CsvConnector.fetch_records` re-scanned from row 0 on every page → O(n²) full sync | **Fixed** |
| F2 | Medium | Documentation referenced test files, a `tools/` directory, `docs/SECRETS.md`, and CI tooling that did not exist | **Fixed** |
| F3 | Medium | Default `csv_allowed_roots` was working-directory-relative, so the documented quick start failed closed | **Fixed** |
| F4 | Medium | Supply-chain tooling claimed in the threat model (semgrep, trivy, SBOM) is absent from CI | Doc corrected; tooling open |
| F5 | Medium | Egress policy is TOCTOU-exposed to DNS rebinding (validates one resolution, connects with another) | Documented; fix open |
| F6 | Low | Metrics are recorded in-process but no scrape endpoint exists; `METRICS_ENABLED` is unused | Doc corrected; fix open |
| F7 | Low | No container, compose, or orchestration artifacts; no image scanning target | Open |
| F8 | Low | Frontend test coverage is one file (API client); no component or page tests | Open |
| F9 | Low | Rate limiter fails open to a per-process in-memory backend when Redis is unavailable | Documented; accepted for now |
| F10 | Info | No pull-request template in the repository | Open |

Severity reflects impact on a production deployment, not effort to fix.

## 4. Detailed findings

### F1 — Quadratic CSV pagination (High) — fixed

`CsvConnector.fetch_records(cursor, limit)` opened the file and iterated from row 0 on
every call, skipping `row_index` rows before yielding a page. A full scan of *n* rows in
pages of `p` therefore read O(n²/p) rows.

Measured before the fix: 10k rows → 0.17 s, 20k → 0.49 s, 40k → 1.65 s; the default
`csv_max_rows` is 1,000,000 and the full 300k-row sync took **72.73 s**. The growth is
visibly super-linear, which is the signature of the defect rather than of raw I/O.

The fix adds a `byte_offset` to `CsvCursor`, captured from `tell()` at a row boundary, and
resumes by seeking to it. The reader is fed a line generator so `tell()` stays usable
(`csv.reader` disables `tell()` when it iterates a file object directly). A checkpoint
with no offset, or one whose fingerprint no longer matches, falls back to row-skipping, so
a pre-upgrade checkpoint keeps working and a changed file restarts cleanly.

Measured after the fix: 10k → 0.21 s, 20k → 0.40 s, 40k → 0.81 s, 300k → **2.05 s**
(~50k rows/s, linear). That is a **35× improvement** on the 300k case.

Regression tests added: `test_pagination_resumes_by_seek_not_rescan`,
`test_offset_less_checkpoint_still_resumes_correctly`, and
`test_full_scan_reads_each_row_once` (asserts each row is examined exactly once).

### F2 — Documentation drift (Medium) — fixed

The threat model cited test files that do not exist (`test_rls.py`,
`test_security_authz.py`, `test_secrets.py`, `test_security_secrets_never_leak.py`,
`test_security_injection.py`, `test_webhooks.py`, `test_authn.py`,
`test_security_rate_limits.py`, `test_api_bounds.py`, `test_config_production_guard.py`,
`test_security_csrf.py`, `test_record_envelope.py`, `test_sync_service.py`,
`test_errors.py`) and named test functions that do not exist (`::test_audit_immutable`,
`::test_csv_formula_injection_not_rendered`, `::test_open_redirect_blocked`). The
architecture document referenced a `tools/` directory and a `LocalTestReceiver` that do
not exist. `docs/SECRETS.md` was cited by source code but absent. `db/models.py` pointed at
a `humisense.db.tenancy` module that does not exist. `CANARY.md` cited
`backend/tests/data/synthetic_historian.csv`, which is actually `data/synthetic/`.

For a security document, this is not cosmetic. A reviewer who follows the citations to
verify the controls finds nothing, which erodes trust in the claims that *are* true. Every
reference has been repointed to the real file and function, and each was re-verified to
resolve. `docs/SECRETS.md` was written from the implementation. The T19 open-redirect
entry described a mitigation for a parameter that does not exist and was rewritten to
state the real posture. The T13 supply-chain entry claimed tooling that is not in CI (see
F4) and was corrected.

### F3 — Copy-and-run defect (Medium) — fixed

The default `csv_allowed_roots` was `./data/synthetic`, resolved relative to the process
working directory. The documented quick start runs commands from `backend/`, so the root
resolved to `backend/data/synthetic`, which does not exist. `doctor` reported
`csv root: FAILED ./data/synthetic (PathNotAllowedError)` and `demo` failed with
"fixture not found". The connector correctly failed closed, but the documented workflow
was broken out of the box.

Relative roots are now anchored to the repository root (three levels above `config.py`),
so the documented commands work from any directory. Absolute roots are passed through
unchanged. Verified: `doctor` now reports `csv root: ok` with zero problems and no
`CSV_ALLOWED_ROOTS` set, run from `backend/`. Regression tests added:
`test_relative_csv_roots_are_anchored_to_the_repository_root` and
`test_absolute_csv_roots_are_left_unchanged`.

### F4 — Supply-chain tooling claimed but absent (Medium) — documentation corrected

`docs/THREAT_MODEL.md` (T13) claimed `semgrep`, `trivy`, and SBOM generation in CI.
`.github/workflows/ci.yml` runs `pip-audit`, `npm audit`, `bandit`, `ruff`, `mypy`, and
`gitleaks` — and none of the three claimed extras. There are no container images in the
repository, so image scanning has no target yet. The documentation now states what is
actually enforced and names the gaps. Adding `semgrep` (SAST depth) and SBOM generation
(`syft`/`cyclonedx`) is recommended before a pilot.

### F5 — Egress DNS rebinding (Medium) — documented, fix open

`EgressPolicy.validate()` resolves the hostname and checks the resulting addresses, but
then hands the *hostname* to `httpx`, which resolves it again at connect time. An attacker
controlling DNS can answer the validation lookup with a public address and the connection
lookup with a private one (TOCTOU). This is the classic DNS-rebinding gap and is called
out explicitly in the OWASP SSRF Prevention Cheat Sheet. The prior documentation
described this as "mitigated"; it is not. The residual-risk text now states it accurately.

The correct fix is to connect to the validated IP while preserving the hostname for the
`Host` header, TLS SNI, and certificate verification. The default posture (HTTPS required,
destination allowlist) meaningfully raises the bar, which is why this is Medium rather
than High.

### F6 — Metrics recorded but not observable (Low) — documentation corrected

`Metrics` records counters and histograms, but no route exposes them (`/metrics` does not
exist) and `METRICS_ENABLED` is defined but never read. Operators therefore cannot scrape
the data the code collects. The architecture document now says so. Either add an
authenticated scrape endpoint or drop the claim; leaving the flag unread is a trap.

### F7 — No deployment artifacts (Low) — open

There is no `Dockerfile`, compose file, or orchestration manifest. The architecture
document refers to "non-root minimal container images" that do not exist in the
repository. For a "production-readiness candidate" this is a real gap: there is no
reproducible way to build and run the service outside a developer machine, and no image to
scan. This is the largest remaining production-readiness item after F5.

### F8 — Thin frontend test coverage (Low) — open

Frontend tests are one file (`frontend/src/api/client.test.ts`, 8 tests) covering the API
client. The presentation helpers, hooks, and pages have no tests. The frontend is not
where the security boundary lives (the API is), which is why this is Low, but a regression
in the client's CSRF header or error handling would not be caught.

### F9 — Rate limiter fails open (Low) — documented

When the Redis backend errors, the limiter logs `rate_limit_degraded` and falls back to a
per-process in-memory counter. Under multiple workers this weakens global limits to
per-process limits, i.e. it fails open. This is a defensible availability-over-strictness
tradeoff for Layer 1, but it must be a conscious one and should be alerted on. It is
covered by `test_limiter_falls_back_when_the_backend_fails`.

### F10 — No pull-request template (Info) — open

The repository has no `.github/PULL_REQUEST_TEMPLATE`. Nothing enforces a review checklist
for changes that touch the security boundary.

## 5. Verified strengths

These were confirmed by independent reproduction, not by reading tests.

- **Tenant isolation in depth.** Application-layer filters *and* PostgreSQL RLS with
  `FORCE` on every tenant table, driven by a transaction-local `app.current_org` GUC. A
  query with no tenant context returns zero rows. Cross-tenant reads return 404 without
  disclosing existence.
- **Deny-by-default authorization** in a shared service layer used by both the API and the
  worker, so a new endpoint cannot accidentally skip it.
- **Secrets.** AES-256-GCM with a 12-byte per-write nonce, tenant and name bound into the
  AEAD associated data, key version stored per row, and identical error handling for wrong
  key / wrong tenant / tampering so a failure is not an oracle. Stored values are
  ciphertext only.
- **Egress policy.** Deny-by-default, blocks loopback, private, link-local, reserved,
  multicast, unspecified, and cloud-metadata addresses; HTTPS-only by default; no
  caller-supplied `Host`/`Authorization` override; redirects not followed by the ingestion
  client.
- **Audit immutability** enforced by a database trigger that rejects `UPDATE` and
  `DELETE`, with the application role lacking the grant.
- **Idempotency** via a uniqueness constraint over `(connection_id, source_record_id,
  content_hash)`. Re-running an identical sync is a no-op; verified 500 → 500, 0 new.
- **Bounded streaming.** Memory does not grow with file size; verified at 300k rows.
- **Error taxonomy** with stable machine codes and safe public messages, a global handler
  that never leaks a traceback, and correlation ids on every response.
- **Secure response headers** including a strict CSP, HSTS in production, and a
  trusted-proxy model that ignores `X-Forwarded-For` unless the topology is declared.

## 6. Non-claims and residual risk

- No penetration test has been performed. The README and threat model say so; that
  honesty is correct and should be preserved.
- No HA or multi-region claim is made. Single-region, single-database.
- Layer 1 has no LLM and no agent, so prompt injection has no surface yet; the envelope
  carries provenance for the layer that will.
- DNS rebinding (F5) remains an open, documented gap.

## 7. Prioritized remediation

**P1 — before a controlled pilot**
1. F5: close the DNS-rebinding gap (connect to the validated IP; preserve hostname for
   Host/SNI/cert verification). Add a test that forces a differing second resolution.
2. F7: add a container build and a compose file so the service is deployable and scannable;
   run as a non-root user.
3. F10: add a pull-request template with an explicit security-boundary checklist.

**P2 — before multi-tenant enterprise data**
4. F4: add `semgrep` and SBOM generation to CI; add image scanning once F7 lands.
5. F6: expose metrics on an authenticated endpoint (or remove the dead flag).
6. F9: alert on `rate_limit_degraded`, and document the fail-open behaviour as accepted.
7. Commission the third-party penetration test the docs already promise.

**P3 — hygiene**
8. F8: add tests for the frontend presentation helpers and the auth/CSRF client paths.
9. Keep the documentation-to-code reference check that this audit ran; wire it into CI so
   a cited-but-missing test file fails the build.

## 8. Change log for this audit

Fixes are in this change set; each carries a regression test.

- F1: `backend/src/humisense/connectors/csv_connector.py` — byte-offset pagination;
  tests in `backend/tests/test_csv_connector.py`.
- F3: `backend/src/humisense/config.py` — anchor relative CSV roots to the repository root;
  tests in `backend/tests/test_config_hardening.py`.
- F2/F4/F6: `docs/THREAT_MODEL.md`, `docs/ARCHITECTURE.md`, `docs/CANARY.md`,
  `backend/src/humisense/db/models.py`, `backend/src/humisense/devtools/cli.py`; new
  `docs/SECRETS.md`.
