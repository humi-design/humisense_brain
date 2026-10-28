"""Rate limiting, resource bound, and webhook signature tests.

Covers T9 (denial of service and resource exhaustion) and the webhook controls required
by T8.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from humisense.errors import AuthenticationError, RateLimitedError
from humisense.security.rate_limit import (
    InMemoryRateLimitBackend,
    RateLimiter,
    build_rate_limiter,
)
from humisense.security.webhooks import (
    compute_signature,
    verify_signature,
)

pytestmark = pytest.mark.security


# --- Rate limiter --------------------------------------------------------


async def test_requests_within_the_budget_are_allowed() -> None:
    limiter = RateLimiter(InMemoryRateLimitBackend())
    for _ in range(5):
        decision = await limiter.check(
            scope="write", identifier="tenant-a", limit=5, window_seconds=60
        )
        assert decision.allowed is True
    assert decision.remaining == 0


async def test_exceeding_the_budget_is_refused() -> None:
    """T9: an expensive endpoint must not be callable without bound."""
    limiter = RateLimiter(InMemoryRateLimitBackend())
    for _ in range(5):
        await limiter.check(scope="write", identifier="tenant-a", limit=5, window_seconds=60)

    with pytest.raises(RateLimitedError):
        await limiter.check(scope="write", identifier="tenant-a", limit=5, window_seconds=60)


async def test_budgets_are_isolated_per_identifier() -> None:
    """One tenant exhausting its budget must not deny another tenant."""
    limiter = RateLimiter(InMemoryRateLimitBackend())
    for _ in range(3):
        await limiter.check(scope="sync", identifier="tenant-a", limit=3, window_seconds=60)

    with pytest.raises(RateLimitedError):
        await limiter.check(scope="sync", identifier="tenant-a", limit=3, window_seconds=60)

    decision = await limiter.check(scope="sync", identifier="tenant-b", limit=3, window_seconds=60)
    assert decision.allowed is True


async def test_budgets_are_isolated_per_scope() -> None:
    limiter = RateLimiter(InMemoryRateLimitBackend())
    for _ in range(2):
        await limiter.check(scope="sync", identifier="tenant-a", limit=2, window_seconds=60)

    with pytest.raises(RateLimitedError):
        await limiter.check(scope="sync", identifier="tenant-a", limit=2, window_seconds=60)

    # A different operation still has its own budget.
    assert (
        await limiter.check(scope="test", identifier="tenant-a", limit=2, window_seconds=60)
    ).allowed


async def test_disabled_limiter_allows_everything() -> None:
    limiter = RateLimiter(InMemoryRateLimitBackend(), enabled=False)
    for _ in range(100):
        assert (
            await limiter.check(scope="write", identifier="tenant-a", limit=1, window_seconds=60)
        ).allowed


async def test_limiter_falls_back_when_the_backend_fails() -> None:
    """A Redis outage must degrade to in-process limits, not to no limits."""

    class BrokenBackend:
        async def increment(self, key: str, window_seconds: int) -> tuple[int, int]:
            raise ConnectionError("redis is unreachable")

    limiter = RateLimiter(BrokenBackend())  # type: ignore[arg-type]
    assert limiter.degraded is False

    # The first call degrades; the limit still applies from the fallback.
    await limiter.check(scope="write", identifier="tenant-a", limit=2, window_seconds=60)
    assert limiter.degraded is True
    await limiter.check(scope="write", identifier="tenant-a", limit=2, window_seconds=60)
    with pytest.raises(RateLimitedError):
        await limiter.check(scope="write", identifier="tenant-a", limit=2, window_seconds=60)


async def test_window_expiry_releases_the_budget() -> None:
    limiter = RateLimiter(InMemoryRateLimitBackend())
    await limiter.check(scope="write", identifier="tenant-a", limit=1, window_seconds=1)
    with pytest.raises(RateLimitedError):
        await limiter.check(scope="write", identifier="tenant-a", limit=1, window_seconds=1)

    # A one-second window has elapsed.
    time.sleep(1.1)
    assert (
        await limiter.check(scope="write", identifier="tenant-a", limit=1, window_seconds=1)
    ).allowed


async def test_decision_reports_the_remaining_budget() -> None:
    limiter = RateLimiter(InMemoryRateLimitBackend())
    first = await limiter.check(scope="write", identifier="tenant-a", limit=10, window_seconds=60)
    second = await limiter.check(scope="write", identifier="tenant-a", limit=10, window_seconds=60)
    assert first.remaining == 9
    assert second.remaining == 8
    assert second.limit == 10
    assert second.reset_after_seconds > 0


async def test_in_memory_backend_prunes_old_windows() -> None:
    backend = InMemoryRateLimitBackend()
    await backend.increment("rl:test:a", 1)
    time.sleep(1.1)
    # A later call prunes the expired entry rather than growing without bound.
    count, _ = await backend.increment("rl:test:b", 1)
    assert count == 1


def test_build_rate_limiter_returns_an_enabled_limiter() -> None:
    limiter = build_rate_limiter("redis://localhost:6379/0", enabled=True)
    assert limiter.enabled is True


# --- Webhook signatures --------------------------------------------------


def test_valid_signature_is_accepted() -> None:
    body = b'{"event": "historian.updated"}'
    timestamp = str(int(time.time()))
    signature = compute_signature("a-signing-secret", timestamp, body)
    verify_signature(secret="a-signing-secret", body=body, signature=signature, timestamp=timestamp)


def test_signature_is_case_insensitive_in_the_header() -> None:
    body = b"{}"
    timestamp = str(int(time.time()))
    signature = compute_signature("secret", timestamp, body).upper()
    verify_signature(secret="secret", body=body, signature=signature, timestamp=timestamp)


def test_missing_signature_is_refused() -> None:
    """T8: an unsigned request must not be trusted."""
    with pytest.raises(AuthenticationError):
        verify_signature(
            secret="secret", body=b"{}", signature=None, timestamp=str(int(time.time()))
        )


def test_missing_timestamp_is_refused() -> None:
    with pytest.raises(AuthenticationError):
        verify_signature(secret="secret", body=b"{}", signature="abc", timestamp=None)


def test_wrong_signature_is_refused() -> None:
    with pytest.raises(AuthenticationError):
        verify_signature(
            secret="secret",
            body=b"{}",
            signature="0" * 64,
            timestamp=str(int(time.time())),
        )


def test_signature_from_a_different_secret_is_refused() -> None:
    body = b"{}"
    timestamp = str(int(time.time()))
    signature = compute_signature("attacker-secret", timestamp, body)
    with pytest.raises(AuthenticationError):
        verify_signature(secret="real-secret", body=body, signature=signature, timestamp=timestamp)


def test_body_tampering_invalidates_the_signature() -> None:
    timestamp = str(int(time.time()))
    signature = compute_signature("secret", timestamp, b'{"amount": 1}')
    with pytest.raises(AuthenticationError):
        verify_signature(
            secret="secret",
            body=b'{"amount": 1000000}',
            signature=signature,
            timestamp=timestamp,
        )


def test_stale_timestamp_is_refused() -> None:
    """T8: an old capture must not be replayable indefinitely."""
    body = b"{}"
    old_timestamp = str(int(time.time()) - 3600)
    signature = compute_signature("secret", old_timestamp, body)
    with pytest.raises(AuthenticationError):
        verify_signature(secret="secret", body=body, signature=signature, timestamp=old_timestamp)


def test_future_timestamp_is_refused() -> None:
    body = b"{}"
    future = str(int(time.time()) + 3600)
    signature = compute_signature("secret", future, body)
    with pytest.raises(AuthenticationError):
        verify_signature(secret="secret", body=body, signature=signature, timestamp=future)


def test_non_numeric_timestamp_is_refused() -> None:
    with pytest.raises(AuthenticationError):
        verify_signature(secret="secret", body=b"{}", signature="abc", timestamp="not-a-number")


def test_replay_within_the_window_is_a_known_residual_risk() -> None:
    """Documented limitation: the timestamp window narrows replay but does not prevent it.

    A nonce store is required before an inbound webhook route is exposed. This test
    records the current behaviour so the gap is explicit rather than assumed closed.
    """
    body = b"{}"
    timestamp = str(int(time.time()))
    signature = compute_signature("secret", timestamp, body)
    for _ in range(3):
        verify_signature(secret="secret", body=body, signature=signature, timestamp=timestamp)


# --- API-level rate limiting --------------------------------------------


async def test_sync_endpoint_is_rate_limited(
    app_client: Any, container: Container, organization: uuid.UUID, csv_root: Path
) -> None:
    """T9: a resource-intensive endpoint enforces its budget."""
    from humisense.services.connections import ConnectionDraft
    from tests.conftest import (
        make_principal,
        organization_slug,
        sign_in,
        synthetic_rows,
        write_csv,
    )

    container.settings.rate_limit_sync_per_hour = 2
    principal = await make_principal(container, organization, role="owner", subject="rl-sync")
    write_csv(csv_root / "a.csv", synthetic_rows(2))
    connection = await container.connections.create_connection(
        principal,
        ConnectionDraft(
            connector_type="csv",
            name="rl",
            description=None,
            config={"path": "a.csv"},
            credentials={},
        ),
    )

    session = await sign_in(
        app_client,
        subject="rl-sync",
        organization_slug=await organization_slug(container, organization),
    )
    headers = {"X-CSRF-Token": session["csrf_token"]}

    statuses = []
    for _ in range(4):
        response = await app_client.post(
            f"/api/v1/connections/{connection.id}/sync", headers=headers
        )
        statuses.append(response.status_code)
        # A queued job must be finalized before the next request, otherwise the
        # concurrency guard would answer 409 instead of exercising the rate limit.
        if response.status_code in {200, 202}:
            for job in await container.sync.list_jobs(organization, connection.id, limit=10):
                if job.status.value in {"queued", "running"}:
                    await container.sync.run_job(organization, job.id)

    assert 429 in statuses, statuses
    limited = [code for code in statuses if code == 429]
    assert len(limited) >= 1


async def test_write_endpoint_is_rate_limited(
    app_client: Any, container: Container, organization: uuid.UUID, csv_root: Path
) -> None:
    from tests.conftest import (
        make_principal,
        organization_slug,
        sign_in,
        synthetic_rows,
        write_csv,
    )

    container.settings.rate_limit_writes_per_minute = 3
    await make_principal(container, organization, role="owner", subject="rl-write")
    write_csv(csv_root / "a.csv", synthetic_rows(2))
    session = await sign_in(
        app_client,
        subject="rl-write",
        organization_slug=await organization_slug(container, organization),
    )
    headers = {"X-CSRF-Token": session["csrf_token"]}

    statuses = []
    for index in range(8):
        response = await app_client.post(
            "/api/v1/connections",
            headers=headers,
            json={
                "connector_type": "csv",
                "name": f"rl-{index}",
                "config": {"path": "a.csv"},
            },
        )
        statuses.append(response.status_code)

    assert 429 in statuses, statuses


async def test_rate_limit_response_is_well_formed(
    app_client: Any, container: Container, organization: uuid.UUID, csv_root: Path
) -> None:
    from tests.conftest import (
        make_principal,
        organization_slug,
        sign_in,
        synthetic_rows,
        write_csv,
    )

    container.settings.rate_limit_writes_per_minute = 1
    await make_principal(container, organization, role="owner", subject="rl-shape")
    write_csv(csv_root / "a.csv", synthetic_rows(2))
    session = await sign_in(
        app_client,
        subject="rl-shape",
        organization_slug=await organization_slug(container, organization),
    )
    headers = {"X-CSRF-Token": session["csrf_token"]}

    for index in range(4):
        response = await app_client.post(
            "/api/v1/connections",
            headers=headers,
            json={"connector_type": "csv", "name": f"x-{index}", "config": {"path": "a.csv"}},
        )
        if response.status_code == 429:
            body = response.json()
            assert body["error"]["code"] == "rate_limited"
            assert "retry-after" in {key.lower() for key in response.headers}
            return
    raise AssertionError("the rate limit was never reached")


from pathlib import Path  # noqa: E402

from humisense.container import Container  # noqa: E402
