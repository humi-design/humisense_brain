# Canary Labs integration — status and access requirements

Status: **not configured**. The adapter in `backend/src/humisense/connectors/canary.py`
performs no capture and makes no network call.

This document is the operator-facing record of what is missing. It is served to the
Connector Catalog's "Documentation" link, which reads the manifest's `documentation_url`
(`docs/CANARY.md`).

## Why the adapter does nothing

The Layer 1 brief forbids inventing API endpoints, SDK methods, database tables,
authentication methods, or product capabilities for Canary. Every one of the items below
must be confirmed against an authorized environment before any capture code is written.
None of them is available in this environment, so the adapter reports
`Availability.NOT_CONFIGURED`, declares no capabilities, and refuses every operation with
a typed, non-retryable error rather than emitting plausible-looking code that would fail
against a real historian.

## What must be confirmed before implementation

1. **Supported interface and version.** Which Canary surface is in scope — the Canary
   Views / Web API, the historian's ODBC or SQL interface, or another supported product
   interface — and which version of it. No endpoint or schema is assumed.
2. **Authentication and authorization method.** How the chosen interface authenticates,
   what the least-privilege credential looks like, and how credential rotation is
   performed.
3. **Network and hosting requirements.** Whether the historian is reachable from the
   connector runtime or only from inside the customer network, and which destination and
   port must be added to the egress allowlist (see `docs/ARCHITECTURE.md`).
4. **Retrievable semantics.** The tag, value, timestamp, unit, and quality-code semantics
   that can actually be retrieved, and whether they can be retrieved incrementally.
5. **A test environment.** Access to an authorized historian to verify all of the above
   before the adapter is declared available.

## Current workaround

Use the synthetic historian CSV with the `csv` connector for end-to-end development.
`data/synthetic/synthetic_historian.csv` (used by `python -m humisense.devtools.cli demo`)
and the seeded demo file are clearly labelled synthetic. They must never be presented as live Canary telemetry.

## What will not change when Canary is configured

Enabling Canary does not weaken any existing control. The adapter will run through the
same egress policy, secrets provider, tenant scoping, audit trail, and rate limits as
every other connector, and it will advertise only capabilities that have been implemented
and tested.
