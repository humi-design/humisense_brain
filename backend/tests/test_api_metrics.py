"""Metrics endpoint tests.

Covers F6: the registry was populated but nothing exposed it, and ``metrics_enabled`` was
a flag no code read. These tests assert the endpoint exists, is authenticated, respects
the permission model, and is absent when metrics are disabled.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from humisense.app import create_app
from humisense.container import Container
from tests.conftest import make_principal, organization_slug, sign_in

pytestmark = [pytest.mark.asyncio, pytest.mark.security]


async def test_metrics_requires_authentication(app_client: Any) -> None:
    """The scrape endpoint must not be a public surface."""
    response = await app_client.get("/api/v1/metrics")
    assert response.status_code == 401


async def test_metrics_requires_the_metrics_permission(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    """A viewer must not read process metrics; an admin may."""
    await make_principal(container, organization, role="viewer", subject="metrics-viewer")
    await sign_in(
        app_client,
        subject="metrics-viewer",
        organization_slug=await organization_slug(container, organization),
    )
    denied = await app_client.get("/api/v1/metrics")
    assert denied.status_code == 403

    app_client.cookies.clear()
    await make_principal(container, organization, role="admin", subject="metrics-admin")
    await sign_in(
        app_client,
        subject="metrics-admin",
        organization_slug=await organization_slug(container, organization),
    )
    allowed = await app_client.get("/api/v1/metrics")
    assert allowed.status_code == 200


async def test_metrics_returns_prometheus_exposition(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    """The body is valid Prometheus text and contains the application counters."""
    await make_principal(container, organization, role="owner", subject="metrics-owner")
    await sign_in(
        app_client,
        subject="metrics-owner",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.get("/api/v1/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# TYPE humisense_api_requests_total counter" in body
    # No tenant identifier is a label value, by design.
    assert str(organization) not in body


async def test_metrics_route_is_absent_when_disabled(
    container: Container, test_settings: Any
) -> None:
    """A disabled deployment has no route at all, not a disabled-but-reachable one."""
    test_settings.metrics_enabled = False
    app = create_app(test_settings, container=container)
    app.state.container = container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get("/api/v1/metrics")
    assert response.status_code == 404
