"""Canary Labs historian adapter.

Status: **not_configured**. This adapter is deliberately incapable of capturing data.

Why it is written this way
--------------------------
The Layer 1 brief forbids inventing API endpoints, SDK methods, database tables,
authentication methods, or product capabilities for Canary. Verifying them requires:

1. The supported Canary interface and its version (for example the Canary Views /
   Web API surface, or the historian's ODBC/SQL interface).
2. The authentication and authorization method that interface requires.
3. Network and hosting requirements, including whether the historian must be reached
   inside a customer network rather than over the public internet.
4. The tag, value, timestamp, and metadata semantics that can actually be retrieved,
   and the units and quality codes attached to them.
5. Access to an authorized test environment to verify all of the above.

None of those is available in this environment. Rather than emit plausible-looking code
that would fail against a real historian, the adapter:

* reports ``Availability.NOT_CONFIGURED`` with an explicit ``unavailable_reason``;
* declares no capabilities at all, so it cannot be mistaken for a working integration;
* refuses every operation with a typed, non-retryable error;
* makes no network call, so no unverified endpoint is ever contacted.

The requirements are listed in ``docs/CANARY.md``. Until they are satisfied, use the
synthetic historian CSV with the ``csv`` connector for end-to-end development. The
synthetic file is clearly labelled as synthetic and must never be presented as live
Canary telemetry.
"""

from __future__ import annotations

from typing import Any, ClassVar

from humisense.connectors.base import BaseConnector
from humisense.connectors.types import (
    Availability,
    ConfigField,
    ConfigFieldType,
    ConnectionTestResult,
    ConnectorCapabilities,
    ConnectorManifest,
    Cursor,
    FetchPage,
    ValidatedConfig,
)
from humisense.errors import ConnectorNotConfiguredError

NOT_CONFIGURED_REASON = (
    "The Canary interface has not been verified against an authorized environment. "
    "Required: the supported Canary interface and version, its authentication and "
    "authorization method, the network and hosting requirements, the retrievable tag and "
    "value semantics, and access to a test historian. No endpoint, schema, or "
    "authentication method is assumed."
)


class CanaryConnector(BaseConnector):
    """Placeholder adapter that reports its true, unconfigured state."""

    manifest: ClassVar[ConnectorManifest] = ConnectorManifest(
        connector_type="canary_labs",
        display_name="Canary Labs Historian",
        description=(
            "Industrial historian integration. Not configured in this environment: the "
            "Canary interface has not been verified, so no capture is performed."
        ),
        vendor="Canary Labs",
        version="0.0.0",
        auth_modes=(),
        capabilities=ConnectorCapabilities(
            supports_incremental=False,
            supports_search=False,
            supports_webhook=False,
            supports_write=False,
            supports_live=False,
            supports_health_check=False,
            supports_pagination=False,
            supports_metadata=False,
            supports_timestamps=False,
        ),
        availability=Availability.NOT_CONFIGURED,
        unavailable_reason=NOT_CONFIGURED_REASON,
        record_types=(),
        config_fields=(
            ConfigField(
                name="endpoint",
                label="Historian endpoint",
                field_type=ConfigFieldType.URL,
                required=False,
                help_text=(
                    "Recorded for planning only. It is not contacted, because the "
                    "interface has not been verified."
                ),
                placeholder="https://historian.example.internal",
            ),
            ConfigField(
                name="tag_allowlist",
                label="Tag allowlist",
                field_type=ConfigFieldType.STRING,
                required=False,
                help_text="Comma-separated historian tags to capture, once verified.",
            ),
        ),
        required_setup=(
            "Confirm the supported Canary interface and version.",
            "Confirm the authentication and authorization method.",
            "Confirm network reachability from the connector runtime, and whether the "
            "historian is reachable only from inside the customer network.",
            "Confirm the retrievable tag, value, timestamp, unit, and quality semantics.",
            "Provide an authorized test environment for verification.",
        ),
        documentation_url="docs/CANARY.md",
        synthetic_only=False,
    )

    def validate_config(self, config: dict[str, Any]) -> ValidatedConfig:
        raise ConnectorNotConfiguredError(
            "The Canary integration is not configured in this environment.",
            detail="canary adapter is a not_configured placeholder",
            context={"connector_type": self.manifest.connector_type},
        )

    async def test_connection(self) -> ConnectionTestResult:
        """Report the true state. No network call is made."""
        return ConnectionTestResult(
            ok=False,
            message=NOT_CONFIGURED_REASON,
            error_code="connector_not_configured",
            details={"availability": Availability.NOT_CONFIGURED.value},
        )

    async def fetch_records(self, cursor: Cursor | None, limit: int) -> FetchPage:
        raise ConnectorNotConfiguredError(
            "The Canary integration is not configured in this environment.",
            detail="canary adapter is a not_configured placeholder",
        )

    async def close(self) -> None:
        return None
