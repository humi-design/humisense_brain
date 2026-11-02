# Humisense Brain / OS — Layer 1 Threat Model

Method: asset-centric with STRIDE over the trust boundaries defined in
`docs/ARCHITECTURE.md`. References: OWASP ASVS 5.0, OWASP API Security Top 10 (2023),
OWASP Top 10 (2021), NIST SP 800-207 where relevant.

Every threat below has: mitigation, residual risk, and the test that exercises it.
Test ids refer to files under `backend/tests/`.

## 1. Assets

| Asset | Why it matters |
|---|---|
| A1 Connector credentials (API keys, passwords, tokens) | Direct access to customer business systems |
| A2 Captured source records | May contain commercial, production, or personal data |
| A3 Tenant isolation boundary | Cross-tenant leak is the highest-impact failure |
| A4 Audit trail | Non-repudiation, incident investigation |
| A5 Application secrets (session signing, encryption keys) | Compromise defeats A1/A3 |
| A6 Availability of the sync pipeline | Loss of sensory data |

## 2. Threats, mitigations, tests

### T1 Cross-tenant data exposure
- **Vector** Identifier guessing or tampering to read another tenant's connection, status, records, or audit.
- **Mitigation** Deny-by-default authorization in a service layer used by both API and worker; every query filtered by `organization_id` derived from the verified token; PostgreSQL RLS with transaction-local `app.current_org` as defence in depth; identifiers are UUIDv4 so they are not enumerable.
- **Residual** An authorization bug in a new endpoint. RLS limits blast radius.
- **Tests** `test_security_tenant_isolation.py` (read, update, sync, records, audit), `test_security_rls.py`.

### T2 Broken object-level authorization (BOLA/IDOR)
- **Vector** Authenticated user of tenant A requests `/connections/{id}` owned by tenant B.
- **Mitigation** Object lookups always take `(organization_id, id)`; the service raises `NotFound` for foreign objects so existence is not disclosed. Permission check on the action, not just the object.
- **Residual** None known.
- **Tests** `test_security_tenant_isolation.py::test_get_foreign_connection_404`, `::test_update_foreign_connection_404`.

### T3 Privilege escalation / broken function-level authorization
- **Vector** `viewer` calls `POST /sync`; client sends `role=owner` in a profile update; mass assignment of `organization_id`, `status`, `secret_ref`.
- **Mitigation** Role→permission matrix evaluated server-side on every request; roles come only from `memberships`; write schemas are explicit allowlists that exclude privileged fields; `extra="forbid"` on all request models.
- **Residual** A mis-issued role by an admin is an operational, not technical, failure.
- **Tests** `test_api_security.py::test_viewer_cannot_create_a_connection`, `::test_viewer_can_read_but_not_write`, `::test_privilege_escalation_via_body_is_ignored`.

### T4 Credential theft
- **Vector** Reading the DB, logs, API responses, or the frontend bundle.
- **Mitigation** Credentials stored only via `SecretsProvider`; the DB implementation uses AES-256-GCM (AEAD) with a key held outside the DB; values are write-only in the API; `RedactionFilter` on logs; response models never contain secret material; frontend never receives or stores tokens.
- **Residual** Compromise of the master key plus DB read access.
- **Tests** `test_security_secrets.py` (encryption, key handling, redaction), `test_api_security.py::test_session_token_is_never_returned_in_a_body`.

### T5 SSRF through user-configured connectors
- **Vector** A connection config points at `http://169.254.169.254/`, `http://localhost:5432`, an internal RFC1918 host, or a public host that redirects to one; DNS rebinding.
- **Mitigation** `EgressPolicy` resolves the hostname, rejects loopback/private/link-local/reserved/multicast/unspecified and cloud metadata addresses, allows only `http`/`https`, enforces a per-tenant destination allowlist, revalidates after every redirect, caps redirects, sets connect/read timeouts, caps response size, and forbids caller-supplied `Host`/`Authorization` override.
- **Residual** DNS rebinding: the policy resolves the hostname and validates the resulting addresses, then hands the *hostname* to the HTTP client, which resolves it again at connect time. An attacker who controls DNS can return a public address to the validation lookup and a private address to the connection lookup. This is a genuine TOCTOU gap, not a theoretical one. Closing it requires connecting to the validated IP while preserving the hostname for the `Host` header, TLS SNI, and certificate verification (see the OWASP SSRF Prevention Cheat Sheet, "DNS rebinding"). It is tracked as F5 in `docs/AUDIT-LAYER1.md`. The default posture still requires `https` and a destination allowlist, which raises the bar.
- **Tests** `test_security_ssrf.py`.

### T6 SQL injection
- **Vector** Crafted values in filters, ids, or JSONB config.
- **Mitigation** SQLAlchemy Core/ORM with bound parameters everywhere; no string-built SQL; the only raw SQL is static DDL and a `set_config` call with a bound parameter; strict typing rejects malformed UUIDs before reaching the DB.
- **Residual** None known.
- **Tests** `test_api_security.py::test_sql_injection_in_a_name_is_stored_verbatim`.

### T7 Path traversal and arbitrary file access
- **Vector** `../../etc/passwd`, absolute paths, symlinks escaping the root, null bytes.
- **Mitigation** Configured `allowed_roots`; `resolve(strict=True)` then containment check; symlink targets resolved before the check; NUL rejection; directories only, never globs from user input; no endpoint that lists the filesystem.
- **Residual** A root configured too broadly is an operator error; documented.
- **Tests** `test_security_files.py`.

### T8 Malicious or oversized files
- **Vector** 10 GB CSV, zip bomb, formula injection, malformed encoding, billion-laughs-style expansion, CSV injection on export.
- **Mitigation** File-size cap, row cap, wall-clock processing cap, streaming reads, encoding detection with replacement and explicit failure, no formula evaluation, no macro execution, uploads stored outside the code tree with cleanup; exports prefix `'` on cells starting with `=+-@`.
- **Residual** A file at the cap still consumes bounded CPU; documented.
- **Tests** `test_security_files.py`, `test_csv_connector.py`.

### T9 Webhook spoofing and replay
- **Vector** Forged or replayed inbound webhook.
- **Mitigation** Inbound webhooks are **not implemented** in Layer 1, so the attack surface does not exist. The signature verification helper (`security/webhooks.py`) is implemented and tested with constant-time comparison and a timestamp window, and is unused by any route. Replay protection requires a nonce store when a route is added.
- **Residual** None in Layer 1.
- **Tests** `test_rate_limit.py` (signature verification and replay window).

### T10 OAuth token leakage
- **Vector** Refresh token in logs, URL, or browser storage.
- **Mitigation** Interactive OAuth for connectors is not implemented in Layer 1; the OIDC verifier validates issuer/audience/expiry/nonce and PKCE is enforced by the configured provider. No token is ever written to browser storage or a URL.
- **Residual** None in Layer 1.
- **Tests** `test_api_security.py` (session and identity tests).

### T11 Insecure background jobs
- **Vector** Job payload tampering, cross-tenant job execution, unbounded concurrency, jobs holding tenant context beyond their lifetime.
- **Mitigation** Jobs carry only ids and a correlation id, never credentials or payloads; the worker re-derives tenant context and re-authorizes; per-tenant concurrency and rate limits; tenant context is set inside each transaction and reset after; cancellation is cooperative and checked between batches.
- **Residual** A hard worker kill leaves a stale `running` job until the reaper runs.
- **Tests** `test_sync_resilience.py`, `test_security_tenant_isolation.py::test_worker_scope_is_enforced`.

### T12 Prompt injection in connected content
- **Vector** An email, document, or record contains "ignore previous instructions".
- **Mitigation** Layer 1 has no LLM and no agent. Payloads are stored as opaque data, never executed, never rendered as HTML, and marked `untrusted=true` in envelope metadata. Authorization is enforced outside any future model; the envelope carries provenance so downstream layers can attribute content.
- **Residual** Belongs to Layer 2+; Layer 1 provides the provenance it needs.
- **Tests** `test_api_security.py::test_record_preview_is_marked_untrusted`, `test_security_files.py::test_formula_injection_is_neutralized`.

### T13 Supply-chain compromise
- **Vector** Malicious or vulnerable dependency, typosquat.
- **Mitigation** Pinned and locked dependencies (`uv.lock`, `package-lock.json`); `pip-audit`, `npm audit`, `bandit`, `ruff`, `mypy`, and `gitleaks` run in CI; no postinstall scripts executed from untrusted sources. Not yet in place: `semgrep`, `trivy`/image scanning, and SBOM generation. There are no container images in the repository, so image scanning has no target yet.
- **Residual** Zero-days in pinned versions between scans. The gaps listed above are tracked in `docs/AUDIT-LAYER1.md` (F4, F7).
- **Tests** CI workflow (`.github/workflows/ci.yml`).

### T14 Sensitive information in logs, errors, audit events
- **Vector** A stack trace, a `repr()` of a config dict, a token in a URL.
- **Mitigation** Global exception handler returns a stable error body with a correlation id and no traceback; `RedactionFilter`; connectors raise typed errors with safe messages; audit events record action/outcome/target only.
- **Residual** A new field added without a redaction rule.
- **Tests** `test_security_secrets.py::test_secret_error_does_not_echo_the_credential`, `test_api_security.py::test_error_does_not_leak_internals`.

### T15 Denial of service and resource exhaustion
- **Vector** Sync floods, huge pagination, expensive connectors, many concurrent jobs.
- **Mitigation** Rate limits per principal and per tenant on sensitive endpoints; bounded pagination (`limit<=200`); body size limit; per-tenant job concurrency; connector timeouts, record caps, and memory bounds; Redis-backed counters.
- **Residual** Sustained distributed abuse of the authenticated API is handled at the edge, not in-app; documented.
- **Tests** `test_rate_limit.py`, `test_api_security.py::test_pagination_bounds_are_enforced`, `::test_oversized_body_is_rejected`.

### T16 Unauthorized writes to connected business systems
- **Vector** A connector performs a write it was not authorized for.
- **Mitigation** The SDK's `write_records` capability defaults to `False`; no Layer 1 connector implements writes; the API exposes no write endpoint; the capability flag is asserted in tests; manifests are static and reviewed.
- **Residual** None in Layer 1.
- **Tests** `test_connector_registry.py::test_no_layer1_connector_advertises_write`.

### T17 Insecure authentication configuration reaching production
- **Vector** Dev auth mode or a wildcard CORS origin deployed to production.
- **Mitigation** `Settings.validate_for_production()` fails startup when `APP_ENV=production` and dev auth is enabled, the session secret is default, CORS contains `*`, or OIDC issuer/audience is missing. Exercised in tests.
- **Residual** An operator who sets `APP_ENV=development` in production defeats it; documented as a deployment control.
- **Tests** `test_config_hardening.py`.

### T18 Session and CSRF weaknesses
- **Vector** XSS-driven request forgery, cookie theft, session fixation.
- **Mitigation** Session cookie `HttpOnly`, `Secure` (in production), `SameSite=Lax`; double-submit CSRF token bound to the session and required on all unsafe methods; session id is a 256-bit random value, hashed at rest; rotation on login; server-side expiry and revocation; no token in `localStorage`.
- **Residual** `SameSite=Lax` permits top-level GET navigation; unsafe methods are CSRF-protected regardless.
- **Tests** `test_api_security.py::test_unsafe_method_without_csrf_header_is_rejected`, `::test_unsafe_method_with_wrong_csrf_token_is_rejected`, `::test_sign_in_sets_hardened_cookies`.

### T19 Unvalidated redirects and open redirect
- **Vector** `?next=` to an external host after login.
- **Mitigation** Not applicable: Layer 1 has no post-login redirect parameter. The API returns JSON and sets a cookie; the browser router owns navigation, so there is no server-side redirect target to validate. The only server-side redirect validation that exists is on connector egress (`EgressPolicy.validate_redirect`), which revalidates every redirect target under the same rules as the initial request.
- **Residual** None in Layer 1. Adding a `next`/return-to parameter later reintroduces this threat and must add same-origin enforcement.
- **Tests** `test_security_ssrf.py::test_redirect_target_is_revalidated`, `::test_redirect_is_refused_when_disabled`.

### T20 Audit log tampering
- **Vector** An application user or compromised app credential updates or deletes audit rows.
- **Mitigation** A database trigger rejects `UPDATE` and `DELETE` on `audit_events`; the app role has no such grant; retention is a documented, deliberate operation.
- **Residual** A DB superuser can bypass; that access is restricted operationally.
- **Tests** `test_audit.py::test_audit_records_cannot_be_updated`, `::test_audit_records_cannot_be_deleted`, `test_security_rls.py::test_audit_events_reject_update`.

## 3. Accepted risks and explicit non-claims

- No penetration test has been performed. A third-party test is required before
  real enterprise data is onboarded.
- No HA, DR, or compliance certification is claimed. The deployment is single-node.
- Inbound webhook handling, connector OAuth flows, and write-back are not implemented;
  their threats are documented as not applicable rather than mitigated.
- Canary Labs integration is **not** verified against a live system. No endpoint,
  schema, or authentication method is invented. The adapter reports `not_configured`.

## 4. Verification commands

```bash
cd backend && uv run pytest -q                        # functional + security suite
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run bandit -r src -q
uv run pip-audit
cd ../frontend && npm run typecheck && npm run build && npm audit
```
