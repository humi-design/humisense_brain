"""Prometheus scrape endpoint.

The metrics registry was already populated, but nothing exposed it, so operators could not
read what the code recorded and ``settings.metrics_enabled`` was a flag no code consulted.
This router closes both gaps.

Exposure is deliberate and narrow:

* The endpoint requires authentication and the ``metrics:read`` permission, so it is not a
  public surface. It is not on the liveness/readiness path a load balancer uses.
* It is served only when ``settings.metrics_enabled`` is true. When it is false the route
  is not registered at all, so there is no disabled-but-reachable state.
* Label values are already bounded by the metrics module; no tenant identifier, payload,
  or credential is ever a label or a value.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from humisense.api.deps import require_permission
from humisense.security.permissions import Permission

router = APIRouter(tags=["observability"])


@router.get(
    "/metrics",
    include_in_schema=False,
    dependencies=[Depends(require_permission(Permission.METRICS_READ))],
    summary="Prometheus metrics",
)
async def metrics() -> Response:
    """Return the Prometheus exposition text for the process registry."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
