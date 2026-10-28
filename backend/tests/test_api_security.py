"""API security tests.

Exercises the assembled ASGI application through an HTTP client, so the middleware stack,
the dependency graph, and the exception handlers are all in the path. Covers T1, T2, T3,
T6, T9, T10, T13, and T17.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from humisense.app import create_app
from humisense.container import Container
from humisense.security.identity import Principal
from tests.conftest import (
    make_principal,
    organization_slug,
    sign_in,
    synthetic_rows,
    write_csv,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.security]


# --- Authentication -------------------------------------------------------


async def test_anonymous_request_is_rejected(app_client: Any) -> None:
    """T6: an unauthenticated request must not reach a protected resource."""
    response = await app_client.get("/api/v1/connections")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/connections",
        "/api/v1/connectors",
        "/api/v1/overview",
        "/api/v1/audit-events",
        f"/api/v1/connections/{uuid.uuid4()}",
        f"/api/v1/connections/{uuid.uuid4()}/status",
        f"/api/v1/connections/{uuid.uuid4()}/records",
    ],
)
async def test_every_protected_route_requires_authentication(app_client: Any, path: str) -> None:
    response = await app_client.get(path)
    assert response.status_code == 401


async def test_health_is_public_but_reveals_nothing_sensitive(app_client: Any) -> None:
    response = await app_client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    serialized = str(payload).lower()
    for leaked in ("password", "database_url", "redis", "secret", "token", "host"):
        assert leaked not in serialized


async def test_readiness_reports_dependencies(app_client: Any) -> None:
    response = await app_client.get("/api/v1/health/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["database"] == "ok"
    assert "connectors_loaded" in payload


async def test_invalid_session_cookie_is_rejected(app_client: Any) -> None:
    app_client.cookies.set("humisense_session", "not-a-real-session-token")
    response = await app_client.get("/api/v1/connections")
    assert response.status_code == 401


async def test_sign_in_rejects_unknown_organization(app_client: Any) -> None:
    response = await app_client.post(
        "/api/v1/auth/session",
        json={"subject": "nobody", "organization_slug": "does-not-exist"},
    )
    assert response.status_code == 401


async def test_sign_in_rejects_unknown_fields(app_client: Any) -> None:
    """T10: mass assignment must be impossible; an unknown field is an error."""
    response = await app_client.post(
        "/api/v1/auth/session",
        json={
            "subject": "x",
            "organization_slug": "y",
            "role": "owner",
            "is_admin": True,
        },
    )
    assert response.status_code == 422


async def test_sign_in_sets_hardened_cookies(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="cookie-user")
    response = await app_client.post(
        "/api/v1/auth/session",
        json={
            "subject": "cookie-user",
            "organization_slug": await organization_slug(container, organization),
        },
    )
    assert response.status_code == 200

    session_cookie = _cookie_header(response, "humisense_session")
    assert "httponly" in session_cookie.lower()
    assert "samesite=lax" in session_cookie.lower()
    # The CSRF cookie is readable by design; it is not a credential on its own.
    csrf_cookie = _cookie_header(response, "humisense_csrf")
    assert "httponly" not in csrf_cookie.lower()


async def test_session_token_is_never_returned_in_a_body(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="body-user")
    payload = await sign_in(
        app_client,
        subject="body-user",
        organization_slug=await organization_slug(container, organization),
    )
    assert "session_token" not in payload
    # Only the CSRF token is exposed, and it is not the session credential.
    assert "csrf_token" in payload


async def test_me_returns_server_derived_identity(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="me-user", role="viewer")
    await sign_in(
        app_client,
        subject="me-user",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.get("/api/v1/auth/me")
    assert response.status_code == 200
    payload = response.json()
    assert payload["role"] == "viewer"
    assert payload["organization_id"] == str(organization)
    assert "connections:read" in payload["permissions"]
    assert "connections:create" not in payload["permissions"]


async def test_logout_revokes_the_session(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="logout-user")
    await sign_in(
        app_client,
        subject="logout-user",
        organization_slug=await organization_slug(container, organization),
    )
    assert (await app_client.get("/api/v1/connections")).status_code == 200

    assert (await app_client.post("/api/v1/auth/logout")).status_code == 204
    assert (await app_client.get("/api/v1/connections")).status_code == 401


# --- CSRF ---------------------------------------------------------------


async def test_unsafe_method_without_csrf_header_is_rejected(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    """T9: a cookie-authenticated write must require the CSRF token."""
    await _make_user(container, organization, subject="csrf-user")
    await sign_in(
        app_client,
        subject="csrf-user",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.post(
        "/api/v1/connections",
        json={"connector_type": "csv", "name": "no-csrf", "config": {"path": "a.csv"}},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


async def test_unsafe_method_with_wrong_csrf_token_is_rejected(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="csrf-bad")
    await sign_in(
        app_client,
        subject="csrf-bad",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.post(
        "/api/v1/connections",
        headers={"X-CSRF-Token": "wrong-token-value"},
        json={"connector_type": "csv", "name": "bad-csrf", "config": {"path": "a.csv"}},
    )
    assert response.status_code == 403


async def test_safe_method_does_not_require_csrf(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="csrf-get")
    await sign_in(
        app_client,
        subject="csrf-get",
        organization_slug=await organization_slug(container, organization),
    )
    assert (await app_client.get("/api/v1/connections")).status_code == 200


async def test_csrf_token_endpoint_requires_a_session(app_client: Any) -> None:
    assert (await app_client.get("/api/v1/auth/csrf")).status_code == 401


# --- Authorization -------------------------------------------------------


async def test_viewer_cannot_create_a_connection(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    """T3: authorization is enforced server-side, not by hiding a button."""
    await _make_user(container, organization, subject="viewer-create", role="viewer")
    session = await sign_in(
        app_client,
        subject="viewer-create",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.post(
        "/api/v1/connections",
        headers={"X-CSRF-Token": session["csrf_token"]},
        json={"connector_type": "csv", "name": "denied", "config": {"path": "a.csv"}},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_viewer_can_read_but_not_write(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="viewer-read", role="viewer")
    await sign_in(
        app_client,
        subject="viewer-read",
        organization_slug=await organization_slug(container, organization),
    )
    assert (await app_client.get("/api/v1/connections")).status_code == 200
    assert (await app_client.get("/api/v1/connectors")).status_code == 200
    assert (await app_client.get("/api/v1/audit-events")).status_code == 403


async def test_privilege_escalation_via_body_is_ignored(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    """T3: a client cannot assert a role or a tenant."""
    await _make_user(container, organization, subject="escalate", role="owner")
    session = await sign_in(
        app_client,
        subject="escalate",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.post(
        "/api/v1/connections",
        headers={"X-CSRF-Token": session["csrf_token"]},
        json={
            "connector_type": "csv",
            "name": "escalated",
            "config": {"path": "a.csv"},
            "role": "owner",
            "organization_id": str(uuid.uuid4()),
        },
    )
    # The unknown fields are rejected outright rather than silently ignored.
    assert response.status_code == 422


# --- Cross-tenant at the HTTP layer --------------------------------------


async def test_cross_tenant_connection_read_returns_404(
    app_client: Any,
    container: Container,
    organization: uuid.UUID,
    other_organization: uuid.UUID,
    csv_root: Path,
) -> None:
    """T1/T2: identifiers from another tenant must not be usable."""
    tenant_a = await make_principal(container, organization, role="owner", subject="api-a")
    write_csv(csv_root / "a.csv", synthetic_rows(3))
    from humisense.services.connections import ConnectionDraft

    connection = await container.connections.create_connection(
        tenant_a,
        ConnectionDraft(
            connector_type="csv",
            name="tenant-a",
            description=None,
            config={"path": "a.csv"},
            credentials={},
        ),
    )

    await _make_user(container, other_organization, subject="api-b")
    await sign_in(
        app_client,
        subject="api-b",
        organization_slug=await organization_slug(container, other_organization),
    )

    response = await app_client.get(f"/api/v1/connections/{connection.id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_cross_tenant_list_is_empty(
    app_client: Any,
    container: Container,
    organization: uuid.UUID,
    other_organization: uuid.UUID,
    csv_root: Path,
) -> None:
    tenant_a = await make_principal(container, organization, role="owner", subject="list-a")
    write_csv(csv_root / "a.csv", synthetic_rows(3))
    from humisense.services.connections import ConnectionDraft

    await container.connections.create_connection(
        tenant_a,
        ConnectionDraft(
            connector_type="csv",
            name="tenant-a",
            description=None,
            config={"path": "a.csv"},
            credentials={},
        ),
    )

    await _make_user(container, other_organization, subject="list-b")
    await sign_in(
        app_client,
        subject="list-b",
        organization_slug=await organization_slug(container, other_organization),
    )

    response = await app_client.get("/api/v1/connections")
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


# --- Input validation ----------------------------------------------------


async def test_unknown_connector_type_is_refused(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="bad-connector")
    session = await sign_in(
        app_client,
        subject="bad-connector",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.post(
        "/api/v1/connections",
        headers={"X-CSRF-Token": session["csrf_token"]},
        json={"connector_type": "definitely_not_real", "name": "x", "config": {}},
    )
    assert response.status_code == 503


async def test_sql_injection_in_a_name_is_stored_verbatim(
    app_client: Any, container: Container, organization: uuid.UUID, csv_root: Path
) -> None:
    """T10: parameterized queries make injection inert; the value is data, not SQL."""
    write_csv(csv_root / "a.csv", synthetic_rows(2))
    await _make_user(container, organization, subject="sqli")
    session = await sign_in(
        app_client,
        subject="sqli",
        organization_slug=await organization_slug(container, organization),
    )
    payload = "'; DROP TABLE connections; --"
    response = await app_client.post(
        "/api/v1/connections",
        headers={"X-CSRF-Token": session["csrf_token"]},
        json={"connector_type": "csv", "name": payload, "config": {"path": "a.csv"}},
    )
    assert response.status_code == 201
    assert response.json()["name"] == payload

    # The table still exists and the row is intact.
    listed = await app_client.get("/api/v1/connections")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


async def test_pagination_bounds_are_enforced(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    """T9: an unbounded page size must not be honored."""
    await _make_user(container, organization, subject="paging")
    await sign_in(
        app_client,
        subject="paging",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.get("/api/v1/connections", params={"limit": 100000})
    assert response.status_code == 200
    assert response.json()["limit"] <= container.settings.max_page_size


async def test_negative_offset_is_rejected(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _make_user(container, organization, subject="offset")
    await sign_in(
        app_client,
        subject="offset",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.get("/api/v1/connections", params={"offset": -1})
    assert response.status_code == 422


async def test_oversized_body_is_rejected(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    """T9: an oversized request must be refused before it is parsed."""
    await _make_user(container, organization, subject="bigbody")
    session = await sign_in(
        app_client,
        subject="bigbody",
        organization_slug=await organization_slug(container, organization),
    )
    huge = "x" * (container.settings.max_request_body_bytes + 1024)
    response = await app_client.post(
        "/api/v1/connections",
        headers={"X-CSRF-Token": session["csrf_token"]},
        json={"connector_type": "csv", "name": huge, "config": {}},
    )
    assert response.status_code in {413, 422}


# --- Response hygiene ----------------------------------------------------


async def test_error_response_shape_is_consistent(app_client: Any) -> None:
    response = await app_client.get("/api/v1/connections")
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) >= {"code", "message", "correlation_id"}


async def test_error_does_not_leak_internals(app_client: Any) -> None:
    app_client.cookies.set("humisense_session", "bogus")
    response = await app_client.get("/api/v1/connections")
    serialized = response.text.lower()
    for leaked in ("traceback", "sqlalchemy", "asyncpg", "postgresql", 'file "', "line "):
        assert leaked not in serialized


async def test_unmatched_route_does_not_echo_the_path(app_client: Any) -> None:
    response = await app_client.get("/api/v1/../../etc/passwd")
    assert response.status_code in {404, 400}
    assert "etc/passwd" not in response.text


async def test_security_headers_are_present(app_client: Any) -> None:
    response = await app_client.get("/api/v1/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] in {"DENY", "SAMEORIGIN"}
    assert "content-security-policy" in response.headers
    assert response.headers["referrer-policy"]


async def test_correlation_id_is_returned(app_client: Any) -> None:
    response = await app_client.get("/api/v1/health")
    assert response.headers.get("x-correlation-id")


async def test_client_supplied_correlation_id_is_sanitized(app_client: Any) -> None:
    """T13: a header cannot be used to inject content into logs."""
    response = await app_client.get(
        "/api/v1/health",
        headers={"X-Correlation-Id": "bad id with spaces and\nnewlines"},
    )
    assert response.status_code == 200
    returned = response.headers["x-correlation-id"]
    assert " " not in returned
    assert "\n" not in returned


async def test_docs_are_disabled_when_not_enabled(container: Container, csv_root: Path) -> None:
    """T17: interactive documentation must not be public in production."""
    container.settings.docs_enabled = False
    app = create_app(container.settings, container=container)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        app.state.container = container
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


async def test_cors_is_not_wildcard(
    app_client: Any,
) -> None:
    response = await app_client.get(
        "/api/v1/health", headers={"Origin": "https://evil.example.com"}
    )
    allow_origin = response.headers.get("access-control-allow-origin")
    assert allow_origin != "*"


# --- Record preview ------------------------------------------------------


async def test_record_preview_is_marked_untrusted(
    app_client: Any, container: Container, organization: uuid.UUID, csv_root: Path
) -> None:
    """T13: connected content is data, and the API says so."""
    principal = await make_principal(container, organization, role="owner", subject="records")
    write_csv(csv_root / "a.csv", synthetic_rows(3))
    from humisense.services.connections import ConnectionDraft

    connection = await container.connections.create_connection(
        principal,
        ConnectionDraft(
            connector_type="csv",
            name="records",
            description=None,
            config={"path": "a.csv", "timestamp_column": "timestamp"},
            credentials={},
        ),
    )
    job = await container.sync.create_job(
        organization_id=organization,
        connection_id=connection.id,
        triggered_by=principal.user_id,
        correlation_id="preview",
    )
    await container.sync.run_job(organization, job.id)

    await sign_in(
        app_client,
        subject="records",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.get(f"/api/v1/connections/{connection.id}/records")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert "untrusted" in payload["notice"].lower()
    for item in payload["items"]:
        assert item["untrusted"] is True


async def test_record_preview_truncates_payloads(
    app_client: Any, container: Container, organization: uuid.UUID, csv_root: Path
) -> None:
    """A preview must not become a bulk-export endpoint."""
    principal = await make_principal(container, organization, role="owner", subject="truncate")
    # Each field is capped at 8 KiB by the connector, so the row must carry several wide
    # columns before the serialized payload exceeds the 16 KiB preview cap.
    columns = [f"note_{index}" for index in range(4)]
    header = ",".join(["timestamp", "machine_id", *columns])
    row = ",".join(["2026-01-01T00:00:00Z", "LINE-01", *(["z" * 8000] * 4)])
    (csv_root / "wide.csv").write_text(f"{header}\n{row}\n", encoding="utf-8")
    from humisense.services.connections import ConnectionDraft

    connection = await container.connections.create_connection(
        principal,
        ConnectionDraft(
            connector_type="csv",
            name="wide",
            description=None,
            config={"path": "wide.csv"},
            credentials={},
        ),
    )
    job = await container.sync.create_job(
        organization_id=organization,
        connection_id=connection.id,
        triggered_by=principal.user_id,
        correlation_id="truncate",
    )
    await container.sync.run_job(organization, job.id)

    await sign_in(
        app_client,
        subject="truncate",
        organization_slug=await organization_slug(container, organization),
    )
    response = await app_client.get(f"/api/v1/connections/{connection.id}/records")
    item = response.json()["items"][0]
    assert item["payload_truncated"] is True
    # The cap applies to the serialized preview, so the response stays bounded regardless
    # of how wide the source row was.
    assert len(item["payload"]["_preview"]) <= 16_384
    assert item["payload"]["_truncated"] is True
    # The full row was about 32 KB of data; the preview is bounded near the 16 KiB cap.
    assert len(str(item["payload"])) < 17_000


# --- Helpers -------------------------------------------------------------


async def _make_user(
    container: Container, organization: uuid.UUID, *, subject: str, role: str = "owner"
) -> Principal:
    return await make_principal(container, organization, role=role, subject=subject)


def _cookie_header(response: Any, name: str) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header
    raise AssertionError(f"cookie {name} was not set: {response.headers.get_list('set-cookie')}")
