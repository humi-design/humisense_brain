"""Connector catalog.

The catalog is generated from the registry, so it can never advertise a connector that is
not registered, and it can never omit a capability the connector does not implement. A
``not_configured`` or ``planned`` connector is listed with an explicit reason, because
hiding it would leave operators unable to plan.

Credentials are never part of this response: the manifest describes which fields a
connector needs, not their values.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from humisense.api.deps import PrincipalDep, require_permission
from humisense.api.schemas import ConfigFieldResponse, ConnectorResponse
from humisense.connectors.base import registry
from humisense.connectors.types import ConnectorManifest
from humisense.security.permissions import Permission

router = APIRouter(prefix="/connectors", tags=["connectors"])


def _to_response(manifest: ConnectorManifest) -> ConnectorResponse:
    public = manifest.as_public_dict()
    return ConnectorResponse(
        connector_type=public["connector_type"],
        display_name=public["display_name"],
        description=public["description"],
        vendor=public["vendor"],
        version=public["version"],
        auth_modes=public["auth_modes"],
        capabilities=public["capabilities"],
        availability=public["availability"],
        unavailable_reason=public["unavailable_reason"],
        required_setup=public["required_setup"],
        documentation_url=public["documentation_url"],
        record_types=public["record_types"],
        synthetic_only=public["synthetic_only"],
        config_fields=[ConfigFieldResponse(**field) for field in public["config_fields"]],
    )


@router.get(
    "",
    response_model=list[ConnectorResponse],
    summary="List available connector types",
    dependencies=[Depends(require_permission(Permission.CONNECTORS_READ))],
)
async def list_connectors(
    principal: PrincipalDep,
    availability: Annotated[str | None, Query(max_length=40)] = None,
) -> list[ConnectorResponse]:
    """Return the connector catalog generated from the registry.

    Args:
        availability: Optional filter, for example ``available`` or ``not_configured``.
    """
    del principal  # authorization enforced by the dependency
    manifests = registry.manifests()
    if availability:
        manifests = [m for m in manifests if m.availability.value == availability]
    manifests.sort(key=lambda m: (m.availability.value != "available", m.display_name))
    return [_to_response(manifest) for manifest in manifests]


@router.get(
    "/{connector_type}",
    response_model=ConnectorResponse,
    summary="Describe one connector type",
    dependencies=[Depends(require_permission(Permission.CONNECTORS_READ))],
)
async def get_connector(connector_type: str, principal: PrincipalDep) -> ConnectorResponse:
    """Return one connector's manifest. Unknown types produce a safe 503."""
    del principal
    from humisense.connectors.base import get_manifest

    return _to_response(get_manifest(connector_type))
