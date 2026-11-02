"""Layer 3 HTTP surface: authentication, authorization, tenant isolation, and CSRF.

These drive the assembled ASGI application, so the middleware, dependency graph, and
permission checks are all in the path. The tenant-isolation test is the important one: a
second tenant must not be able to read a first tenant's detection even knowing its id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from humisense.container import Container
from tests.conftest import make_principal, organization_slug, sign_in

pytestmark = [pytest.mark.asyncio]

BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def _signed_in_client(
    app_client: Any, container: Container, organization: uuid.UUID, *, role: str = "owner"
) -> dict[str, str]:
    subject = f"analytics-{role}-{uuid.uuid4().hex[:8]}"
    await make_principal(container, organization, role=role, subject=subject)
    payload = await sign_in(
        app_client,
        subject=subject,
        organization_slug=await organization_slug(container, organization),
    )
    return {"X-CSRF-Token": payload["csrf_token"]}


async def _seed_detection(container: Container, organization: uuid.UUID) -> uuid.UUID:
    """Insert one detection row directly, for read-path tests."""
    detection_id = uuid.uuid4()
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO detection_events (id, organization_id, detection_id, domain, "
                "source_system, entity_id, metric, detection_category, detection_type, "
                "severity, detected_at, algorithm_name, algorithm_version, anomaly_score, "
                "data_quality_status, evidence_references, explanation, limitations, "
                "schema_version, status, group_key, correlation_id, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :org, :det, 'industrial', 's', 'LINE-01', "
                "'temperature', 'point_anomaly', 'z_score', 'high', now(), 'z_score', '1.0.0', "
                "9.0, 'good', '[]', 'spike', '[]', '3.0', 'open', 'k', 'c', now(), now())"
            ),
            {"org": organization, "det": detection_id},
        )
    return detection_id


async def test_anonymous_cannot_read_detections(app_client: Any) -> None:
    assert (await app_client.get("/api/v1/analytics/detections")).status_code == 401


async def test_anonymous_cannot_run_analytics(app_client: Any) -> None:
    assert (await app_client.post("/api/v1/analytics/run", json={})).status_code == 401


async def test_owner_can_list_detections(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _signed_in_client(app_client, container, organization)
    detection_id = await _seed_detection(container, organization)
    response = await app_client.get("/api/v1/analytics/detections")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert any(item["detection_id"] == str(detection_id) for item in body["items"])
    item = next(i for i in body["items"] if i["detection_id"] == str(detection_id))
    assert item["evidence_references"] == []  # a reference list, never a payload
    assert item["algorithm_version"] == "1.0.0"


async def test_viewer_can_read_but_not_run(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    headers = await _signed_in_client(app_client, container, organization, role="viewer")
    assert (await app_client.get("/api/v1/analytics/detections")).status_code == 200
    response = await app_client.post("/api/v1/analytics/run", json={}, headers=headers)
    assert response.status_code == 403


async def test_run_requires_csrf(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _signed_in_client(app_client, container, organization)
    response = await app_client.post("/api/v1/analytics/run", json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


async def test_run_over_an_empty_window_is_accepted_and_reports_no_records(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    headers = await _signed_in_client(app_client, container, organization)
    response = await app_client.post(
        "/api/v1/analytics/run",
        json={"since": BASE.isoformat(), "until": (BASE + timedelta(hours=1)).isoformat()},
        headers=headers,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "skipped"
    assert body["detections"] == 0
    assert any(skip["code"] == "no_records" for skip in body["skips"])


async def test_detection_from_another_tenant_is_not_readable(
    app_client: Any,
    container: Container,
    organization: uuid.UUID,
    other_organization: uuid.UUID,
) -> None:
    """A second tenant cannot read a first tenant's detection, even by its id."""
    detection_id = await _seed_detection(container, organization)
    await _signed_in_client(app_client, container, other_organization)
    response = await app_client.get(f"/api/v1/analytics/detections/{detection_id}")
    assert response.status_code == 404


async def test_detection_list_is_tenant_scoped(
    app_client: Any,
    container: Container,
    organization: uuid.UUID,
    other_organization: uuid.UUID,
) -> None:
    await _seed_detection(container, organization)
    await _signed_in_client(app_client, container, other_organization)
    response = await app_client.get("/api/v1/analytics/detections")
    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_rule_condition_is_validated(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    headers = await _signed_in_client(app_client, container, organization)
    response = await app_client.post(
        "/api/v1/analytics/rules",
        json={
            "name": "bad",
            "metric": "temperature",
            "condition": {"operator": "eval_everything"},
        },
        headers=headers,
    )
    assert response.status_code == 422


async def test_a_valid_rule_is_stored_and_listed(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    headers = await _signed_in_client(app_client, container, organization)
    created = await app_client.post(
        "/api/v1/analytics/rules",
        json={
            "name": "high temperature",
            "metric": "temperature",
            "entity_id": "LINE-01",
            "condition": {"operator": "gt", "operand": "value", "threshold": 90.0},
            "severity": "high",
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["version"] == 1
    listed = await app_client.get("/api/v1/analytics/rules")
    assert listed.status_code == 200
    assert any(rule["name"] == "high temperature" for rule in listed.json())


async def test_quality_endpoint_is_tenant_scoped(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _signed_in_client(app_client, container, organization)
    response = await app_client.get("/api/v1/analytics/quality")
    assert response.status_code == 200
    assert response.json()["total"] == 0
