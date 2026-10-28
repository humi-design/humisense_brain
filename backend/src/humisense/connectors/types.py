"""Connector SDK types.

These are the contracts every connector implements and every consumer depends on. They
are deliberately narrow and typed: no connector returns a bare ``dict``, and no connector
raises an untyped exception.

The separation the platform relies on:

* ``ConnectorManifest``  — declarative metadata, static per connector type.
* ``ConnectorCapabilities`` — what the implementation actually does.
* ``ConnectorContext``  — the execution context handed to a connector instance.
* ``SourceRecordDraft``  — one captured record before persistence.
* ``FetchPage``         — one bounded page of capture results.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"
"""Version of the source-record envelope produced by Layer 1."""

MAX_SOURCE_RECORD_ID_LENGTH = 512
MAX_PAYLOAD_BYTES = 1 * 1024 * 1024
"""Per-record payload cap. A record larger than this is rejected, not truncated."""


class AuthMode(str, Enum):
    """How a connector authenticates to its source system."""

    NONE = "none"
    """Reads from a local, already-authorized location (for example CSV)."""

    API_KEY = "api_key"
    BASIC = "basic"
    BEARER_TOKEN = "bearer_token"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
    """Declared for future use. No Layer 1 connector implements this."""

    SERVICE_ACCOUNT = "service_account"
    """Declared for future use. No Layer 1 connector implements this."""


class Availability(str, Enum):
    """Whether a connector can actually be used in this environment."""

    AVAILABLE = "available"
    """Implemented and verified."""

    NOT_CONFIGURED = "not_configured"
    """Implemented, but required external configuration or verification is missing."""

    UNVERIFIED = "unverified"
    """Implementation exists but has never been validated against a live system."""

    PLANNED = "planned"
    """Catalogued for the roadmap. No implementation exists."""


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    """What a connector implementation can truthfully do.

    Every flag must correspond to implemented and tested behaviour. A flag set to True
    without an implementation is a lie that the platform will propagate to operators.
    """

    supports_incremental: bool = False
    supports_search: bool = False
    supports_webhook: bool = False
    supports_write: bool = False
    supports_live: bool = False
    supports_health_check: bool = False
    supports_pagination: bool = False
    supports_metadata: bool = True
    supports_timestamps: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "supports_incremental": self.supports_incremental,
            "supports_search": self.supports_search,
            "supports_webhook": self.supports_webhook,
            "supports_write": self.supports_write,
            "supports_live": self.supports_live,
            "supports_health_check": self.supports_health_check,
            "supports_pagination": self.supports_pagination,
            "supports_metadata": self.supports_metadata,
            "supports_timestamps": self.supports_timestamps,
        }

    def read_only(self) -> bool:
        return not self.supports_write


class ConfigFieldType(str, Enum):
    STRING = "string"
    SECRET_STRING = "secret_string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ENUM = "enum"
    PATH = "path"
    URL = "url"


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One field in a connector's configuration form.

    The frontend renders these; the backend revalidates against the connector's own
    Pydantic schema. The form is presentation only and never the enforcement point.
    """

    name: str
    label: str
    field_type: ConfigFieldType
    required: bool = True
    help_text: str = ""
    placeholder: str | None = None
    choices: tuple[str, ...] = ()
    max_length: int | None = None
    default: Any = None
    is_secret: bool = False
    """Secret fields are collected separately, written to the secrets provider, and
    never returned in a configuration response."""


@dataclass(frozen=True, slots=True)
class ConnectorManifest:
    """Static metadata describing a connector type."""

    connector_type: str
    display_name: str
    description: str
    vendor: str
    version: str
    auth_modes: tuple[AuthMode, ...]
    capabilities: ConnectorCapabilities
    availability: Availability
    config_fields: tuple[ConfigField, ...] = ()
    unavailable_reason: str | None = None
    required_setup: tuple[str, ...] = ()
    documentation_url: str | None = None
    record_types: tuple[str, ...] = ()
    synthetic_only: bool = False
    """True when the connector is a development fixture, not a real integration."""

    def as_public_dict(self) -> dict[str, Any]:
        """Serialize for the API. Never includes credential material."""
        return {
            "connector_type": self.connector_type,
            "display_name": self.display_name,
            "description": self.description,
            "vendor": self.vendor,
            "version": self.version,
            "auth_modes": [mode.value for mode in self.auth_modes],
            "capabilities": self.capabilities.as_dict(),
            "availability": self.availability.value,
            "unavailable_reason": self.unavailable_reason,
            "required_setup": list(self.required_setup),
            "documentation_url": self.documentation_url,
            "record_types": list(self.record_types),
            "synthetic_only": self.synthetic_only,
            "config_fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "field_type": f.field_type.value,
                    "required": f.required,
                    "help_text": f.help_text,
                    "placeholder": f.placeholder,
                    "choices": list(f.choices),
                    "max_length": f.max_length,
                    "default": f.default,
                    "is_secret": f.is_secret,
                }
                for f in self.config_fields
            ],
        }


class CancellationToken(Protocol):
    """Cooperative cancellation signal.

    A connector checks this between units of work. Cancellation is not preemptive: a
    connector that ignores the token cannot be stopped by the platform, which is why
    every connector in this repository checks it at least once per page.
    """

    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


class NullCancellationToken:
    """A token that is never cancelled. Used in tests and one-shot operations."""

    __slots__ = ()

    @property
    def cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


@dataclass(slots=True)
class ResourceLimits:
    """Bounds applied to one connector execution."""

    max_records: int = 100_000
    max_file_bytes: int = 64 * 1024 * 1024
    max_processing_seconds: float = 120.0
    max_record_bytes: int = MAX_PAYLOAD_BYTES
    page_size: int = 500


@dataclass(slots=True)
class ConnectorContext:
    """The execution context handed to a connector for one operation.

    A connector receives exactly this and nothing else. It has no database session, no
    access to another tenant's credentials, and no way to reach the network except
    through the injected egress policy.
    """

    organization_id: uuid.UUID
    connection_id: uuid.UUID
    connector_type: str
    correlation_id: str
    config: dict[str, Any]
    credentials: dict[str, Any] = field(default_factory=dict)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    cancellation: CancellationToken = field(default_factory=NullCancellationToken)
    egress_policy: Any | None = None
    """An :class:`~humisense.security.egress.EgressPolicy`, or None for connectors that
    make no network calls. A connector that needs the network must fail closed when this
    is None."""

    path_policy: Any | None = None
    """A :class:`~humisense.security.paths.PathPolicy`, or None for connectors that do not
    read the filesystem. A file connector must fail closed when this is None."""

    logger: Any | None = None

    def redacted_config(self) -> dict[str, Any]:
        """Return the configuration with credential-shaped values removed."""
        from humisense.logging_setup import redact_value

        return redact_value(self.config)  # type: ignore[no-any-return]


class SourceRecordDraft(BaseModel):
    """One captured record, validated, before persistence.

    Layer 1 preserves source meaning. Fields are copied, not interpreted: no unit
    conversion, no timestamp inference, no business-semantic normalization. That is
    Layer 2's responsibility.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_record_id: str = Field(min_length=1, max_length=MAX_SOURCE_RECORD_ID_LENGTH)
    record_type: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_timestamp: datetime | None = None

    @field_validator("source_record_id")
    @classmethod
    def _no_control_characters(cls, value: str) -> str:
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("source_record_id must not contain control characters")
        return value

    @field_validator("payload")
    @classmethod
    def _payload_is_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON object")
        return value


@dataclass(slots=True)
class PartialFailure:
    """A record or row that could not be captured, with a safe reason."""

    source_record_id: str | None
    reason_code: str
    safe_message: str
    location: str | None = None
    """A non-sensitive locator such as a row number or a tag name."""


@dataclass(slots=True)
class FetchPage:
    """One bounded page of capture results."""

    records: list[SourceRecordDraft]
    next_cursor: dict[str, Any] | None = None
    has_more: bool = False
    partial_failures: list[PartialFailure] = field(default_factory=list)
    scanned_count: int = 0
    """Items examined, including ones that produced no record. Supports progress and
    rate accounting without implying a record was captured."""


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    """Outcome of a connection test. Safe to return to a client verbatim."""

    ok: bool
    message: str
    latency_ms: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.ok and not self.error_code:
            raise ValueError("a failed ConnectionTestResult must carry an error_code")


@dataclass(frozen=True, slots=True)
class HealthResult:
    """Outcome of a health check. Safe to return to a client verbatim."""

    healthy: bool
    message: str
    checked_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidatedConfig:
    """Result of connector-side configuration validation."""

    values: dict[str, Any]
    warnings: tuple[str, ...] = ()


class Cursor(BaseModel):
    """Opaque pagination checkpoint.

    The platform stores it verbatim and hands it back to the connector. Layer 1 never
    interprets it, so a connector can evolve its cursor without a migration.
    """

    model_config = ConfigDict(extra="allow")

    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SourceRecordEnvelope(BaseModel):
    """The standard envelope transported to Layer 2.

    This is the Layer 1 to Layer 2 contract. It is versioned so Layer 2 can support more
    than one producer version.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: ClassVar[str] = SCHEMA_VERSION

    connector_type: str
    connection_id: uuid.UUID
    organization_id: uuid.UUID
    source_record_id: str
    record_type: str
    source_timestamp: datetime | None = None
    captured_at: datetime
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    correlation_id: str
    untrusted: bool = True
    """Always True. Connected content is data, never instructions. Layer 2 and any future
    model-facing layer must treat it accordingly."""

    def idempotency_key(self) -> str:
        """Return the key Layer 2 uses to deduplicate redelivery."""
        return f"{self.connection_id}:{self.source_record_id}:{self.content_hash}"

    def size_bytes(self) -> int:
        import json

        return len(
            json.dumps(
                {"payload": self.payload, "metadata": self.metadata},
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        )
