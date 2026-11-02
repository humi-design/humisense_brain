"""Connector SDK, registry, and manifest tests."""

from __future__ import annotations

import pytest

from humisense.connectors.base import build_connector, get_manifest, registry
from humisense.connectors.canary import CanaryConnector
from humisense.connectors.csv_connector import CsvConnector
from humisense.connectors.types import (
    Availability,
    ConnectorCapabilities,
    ConnectorContext,
    SourceRecordDraft,
)
from humisense.errors import ConnectorNotConfiguredError, ConnectorUnavailableError


def _context(**overrides: object) -> ConnectorContext:
    import uuid

    defaults: dict[str, object] = {
        "organization_id": uuid.uuid4(),
        "connection_id": uuid.uuid4(),
        "connector_type": "csv",
        "correlation_id": "test-correlation",
        "config": {},
    }
    defaults.update(overrides)
    return ConnectorContext(**defaults)  # type: ignore[arg-type]


def test_registry_contains_expected_connectors() -> None:
    assert set(registry.connector_types()) == {"csv", "canary_labs"}


def test_registry_rejects_unknown_connector_type() -> None:
    with pytest.raises(ConnectorUnavailableError) as excinfo:
        registry.get("../../etc/passwd")
    # The error must not disclose which connector types exist.
    assert "passwd" not in excinfo.value.public_message
    assert excinfo.value.detail == "Unknown connector type"


def test_registry_has_no_dynamic_import_path() -> None:
    """A connector type must be a registry key, never a module or attribute path."""
    for connector_type in registry.connector_types():
        assert "." not in connector_type
        assert "/" not in connector_type


def test_csv_manifest_capabilities_are_truthful() -> None:
    manifest = get_manifest("csv")
    assert manifest.availability is Availability.AVAILABLE
    assert manifest.capabilities.supports_incremental is True
    assert manifest.capabilities.supports_pagination is True
    assert manifest.capabilities.supports_write is False
    assert manifest.capabilities.supports_webhook is False
    assert manifest.capabilities.supports_search is False
    assert manifest.capabilities.supports_live is False


@pytest.mark.security
def test_no_layer1_connector_advertises_write() -> None:
    """T16: a connector must not claim a capability it does not implement."""
    for manifest in registry.manifests():
        assert manifest.capabilities.supports_write is False, manifest.connector_type


@pytest.mark.security
def test_no_layer1_connector_advertises_webhook_or_search() -> None:
    for manifest in registry.manifests():
        assert manifest.capabilities.supports_webhook is False, manifest.connector_type
        assert manifest.capabilities.supports_search is False, manifest.connector_type


def test_manifest_public_dict_has_no_secret_values() -> None:
    """A manifest describes required fields; it must never carry a credential value."""
    public = get_manifest("csv").as_public_dict()
    serialized = str(public)
    assert "password" not in serialized.lower()
    assert "secret_ref" not in serialized


def test_capabilities_round_trip() -> None:
    capabilities = ConnectorCapabilities(supports_incremental=True)
    as_dict = capabilities.as_dict()
    assert set(as_dict) == {
        "supports_incremental",
        "supports_search",
        "supports_webhook",
        "supports_write",
        "supports_live",
        "supports_health_check",
        "supports_pagination",
        "supports_metadata",
        "supports_timestamps",
    }
    assert capabilities.read_only() is True


async def test_write_operation_is_refused_on_read_only_connector() -> None:
    """T16: calling an unimplemented write must fail, not silently succeed."""
    connector = CsvConnector(_context())
    with pytest.raises(ConnectorUnavailableError):
        await connector.write_records([{"a": 1}])


async def test_search_is_refused_when_not_advertised() -> None:
    connector = CsvConnector(_context())
    with pytest.raises(ConnectorUnavailableError):
        await connector.search("query", 10)


async def test_canary_reports_not_configured() -> None:
    """Section 8: the Canary adapter must report its true state."""
    manifest = get_manifest("canary_labs")
    assert manifest.availability is Availability.NOT_CONFIGURED
    assert manifest.unavailable_reason
    assert "verified" in manifest.unavailable_reason
    assert manifest.capabilities.as_dict() == dict.fromkeys(manifest.capabilities.as_dict(), False)
    assert manifest.required_setup


async def test_canary_refuses_to_capture() -> None:
    connector = CanaryConnector(_context(connector_type="canary_labs"))
    with pytest.raises(ConnectorNotConfiguredError):
        await connector.fetch_records(None, 10)
    with pytest.raises(ConnectorNotConfiguredError):
        connector.validate_config({})


async def test_canary_test_connection_reports_failure_without_network() -> None:
    connector = CanaryConnector(_context(connector_type="canary_labs"))
    result = await connector.test_connection()
    assert result.ok is False
    assert result.error_code == "connector_not_configured"
    # No endpoint was contacted; the message explains what is missing.
    assert "not" in result.message.lower()


def test_ensure_available_refuses_not_configured() -> None:
    from humisense.connectors.base import ensure_available

    with pytest.raises(ConnectorUnavailableError):
        ensure_available("canary_labs")


def test_build_connector_returns_typed_instance() -> None:
    connector = build_connector("csv", _context())
    assert isinstance(connector, CsvConnector)
    assert connector.get_capabilities().supports_write is False


def test_source_record_draft_rejects_control_characters() -> None:
    with pytest.raises(ValueError):
        SourceRecordDraft(
            source_record_id="row\x00id",
            record_type="csv_row",
            payload={"a": 1},
        )


def test_source_record_draft_rejects_non_object_payload() -> None:
    with pytest.raises(ValueError):
        SourceRecordDraft(
            source_record_id="row-1",
            record_type="csv_row",
            payload="not-an-object",  # type: ignore[arg-type]
        )


def test_source_record_draft_forbids_extra_fields() -> None:
    with pytest.raises(ValueError):
        SourceRecordDraft(
            source_record_id="row-1",
            record_type="csv_row",
            payload={},
            unexpected="value",  # type: ignore[call-arg]
        )
