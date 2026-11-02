"""Connector base class and the registry.

The registry is a **static allowlist**. Connector implementations are imported by name
from this module. There is no mechanism to load a module, class, or path supplied by a
request, so a compromised configuration cannot turn into arbitrary code execution.

Third-party connectors are treated as untrusted extensions: they receive only a
:class:`~humisense.connectors.types.ConnectorContext`, cannot open a database session,
cannot read another tenant's credentials, and reach the network only through the injected
egress policy.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar, TypeVar

import structlog

from humisense.connectors.types import (
    ConnectionTestResult,
    ConnectorCapabilities,
    ConnectorContext,
    ConnectorManifest,
    Cursor,
    FetchPage,
    HealthResult,
    ValidatedConfig,
)
from humisense.errors import ConnectorConfigError, ConnectorUnavailableError

logger = structlog.get_logger(__name__)


class BaseConnector(ABC):
    """Contract every connector implements.

    Subclasses declare ``manifest`` and implement the abstract methods. Optional
    operations default to an explicit "not supported" failure rather than a silent no-op,
    so a caller that asks for an unsupported capability gets a clear error.
    """

    manifest: ClassVar[ConnectorManifest]

    def __init__(self, context: ConnectorContext) -> None:
        self.context = context
        self._log = structlog.get_logger(
            f"connector.{self.manifest.connector_type}",
            correlation_id=context.correlation_id,
            organization_id=str(context.organization_id),
            connection_id=str(context.connection_id),
        )

    # --- Required ---------------------------------------------------------

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> ValidatedConfig:
        """Validate non-secret configuration.

        Raises:
            ConnectorConfigError: with a message safe to show an operator.
        """

    @abstractmethod
    async def test_connection(self) -> ConnectionTestResult:
        """Probe the source and report whether it is usable.

        Must be bounded in time and must not modify the source.
        """

    @abstractmethod
    async def fetch_records(
        self,
        cursor: Cursor | None,
        limit: int,
    ) -> FetchPage:
        """Capture up to ``limit`` records starting after ``cursor``.

        Implementations must respect ``self.context.limits`` and check
        ``self.context.cancellation`` at least once per page.
        """

    def get_capabilities(self) -> ConnectorCapabilities:
        """Return the capabilities of this implementation."""
        return self.manifest.capabilities

    # --- Optional ---------------------------------------------------------

    async def health_check(self) -> HealthResult:
        """Return a lightweight health signal.

        Defaults to delegating to :meth:`test_connection`, which is correct for
        connectors whose only meaningful health signal is "can I reach the source".
        """
        result = await self.test_connection()
        return HealthResult(
            healthy=result.ok,
            message=result.message,
            details=result.details,
            checked_at=None,
        )

    async def close(self) -> None:
        """Release any resources held by this instance.

        Called by the runtime in a ``finally`` block. Implementations that hold file
        handles or HTTP clients must override this.
        """
        return None

    # --- Capability guards ------------------------------------------------

    def _require_capability(self, capability: str) -> None:
        if not getattr(self.manifest.capabilities, capability, False):
            raise ConnectorUnavailableError(
                detail=f"Connector {self.manifest.connector_type} does not support {capability}",
                context={"connector_type": self.manifest.connector_type},
            )

    async def search(self, query: str, limit: int) -> FetchPage:
        """Optional. Advertise ``supports_search`` before implementing."""
        self._require_capability("supports_search")
        raise ConnectorUnavailableError(detail="search is not implemented")

    async def write_records(self, records: list[Any]) -> int:
        """Optional. Layer 1 connectors are read-only and must not implement this."""
        self._require_capability("supports_write")
        raise ConnectorUnavailableError(detail="write_records is not implemented")

    async def subscribe(self, handler: Any) -> None:
        """Optional. Advertise ``supports_webhook`` before implementing."""
        self._require_capability("supports_webhook")
        raise ConnectorUnavailableError(detail="subscribe is not implemented")

    # --- Helpers for subclasses ------------------------------------------

    def check_cancellation(self) -> None:
        self.context.cancellation.raise_if_cancelled()

    def elapsed_seconds(self, started: float) -> float:
        return time.monotonic() - started

    def enforce_time_budget(self, started: float) -> None:
        """Raise when the connector has exceeded its processing-time budget."""
        from humisense.errors import ConnectorResourceLimitError

        elapsed = self.elapsed_seconds(started)
        if elapsed > self.context.limits.max_processing_seconds:
            raise ConnectorResourceLimitError(
                detail="Connector exceeded its processing-time budget",
                context={"elapsed_seconds": round(elapsed, 3)},
            )

    @staticmethod
    def config_error(message: str, *, field: str | None = None) -> ConnectorConfigError:
        """Build a configuration error whose message is safe for an operator to read."""
        return ConnectorConfigError(
            message if field is None else f"{field}: {message}",
            detail=f"connector configuration rejected ({field or 'general'})",
            context={"field": field} if field else {},
        )


ConnectorClass = type[BaseConnector]
T = TypeVar("T", bound=ConnectorClass)


class ConnectorRegistry:
    """Static registry of connector types.

    Populated at import time from an explicit allowlist. ``register`` is not part of the
    public API surface reachable from a request.
    """

    def __init__(self) -> None:
        self._connectors: dict[str, ConnectorClass] = {}

    def register(self, connector_class: ConnectorClass) -> ConnectorClass:
        """Register a connector class under its manifest's connector_type."""
        manifest = getattr(connector_class, "manifest", None)
        if manifest is None:
            raise ValueError(f"{connector_class.__name__} does not declare a manifest")
        if not isinstance(manifest, ConnectorManifest):
            raise ValueError(f"{connector_class.__name__}.manifest must be a ConnectorManifest")
        connector_type = manifest.connector_type
        if not connector_type or not connector_type.replace("_", "").isalnum():
            raise ValueError(f"invalid connector_type {connector_type!r}")
        if connector_type in self._connectors:
            raise ValueError(f"connector_type {connector_type!r} is already registered")
        self._connectors[connector_type] = connector_class
        return connector_class

    def get(self, connector_type: str) -> ConnectorClass:
        """Return the connector class, or raise a safe error.

        The error never reveals which connector types exist, so an unauthenticated probe
        cannot enumerate the catalog.
        """
        try:
            return self._connectors[connector_type]
        except KeyError as exc:
            raise ConnectorUnavailableError(
                detail="Unknown connector type",
                context={"connector_type": connector_type},
            ) from exc

    def has(self, connector_type: str) -> bool:
        return connector_type in self._connectors

    def manifests(self) -> list[ConnectorManifest]:
        return [cls.manifest for cls in self._connectors.values()]

    def connector_types(self) -> list[str]:
        return sorted(self._connectors)

    def __len__(self) -> int:
        return len(self._connectors)


registry = ConnectorRegistry()


def build_connector(connector_type: str, context: ConnectorContext) -> BaseConnector:
    """Instantiate a registered connector for one operation."""
    connector_class = registry.get(connector_type)
    return connector_class(context)


def get_manifest(connector_type: str) -> ConnectorManifest:
    return registry.get(connector_type).manifest


def ensure_available(connector_type: str) -> ConnectorManifest:
    """Return the manifest, refusing connector types that are not usable here."""
    from humisense.connectors.types import Availability

    manifest = get_manifest(connector_type)
    if manifest.availability in {Availability.PLANNED, Availability.NOT_CONFIGURED}:
        raise ConnectorUnavailableError(
            manifest.unavailable_reason or "Connector is not configured in this environment",
            detail=f"connector_type={connector_type} availability={manifest.availability.value}",
        )
    return manifest


def load_builtin_connectors() -> None:
    """Import and register the built-in connectors.

    Called once at application startup. The imports are explicit so the set of
    registrable connector types is visible in a code review and cannot be influenced by
    configuration.
    """
    from humisense.connectors.canary import CanaryConnector
    from humisense.connectors.csv_connector import CsvConnector

    for connector_class in (CsvConnector, CanaryConnector):
        if not registry.has(connector_class.manifest.connector_type):
            registry.register(connector_class)
