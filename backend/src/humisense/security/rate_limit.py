"""Rate limiting.

A Redis fixed-window counter is used for correctness across workers, with an in-process
fallback so that a Redis outage degrades rate limiting rather than taking the API down.
The fallback is logged loudly; it is a deliberate availability trade-off and it is
documented in the residual risk section.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import redis.asyncio as aioredis
import structlog

from humisense.errors import RateLimitedError

logger = structlog.get_logger(__name__)

_FIXED_WINDOW_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    reset_after_seconds: int
    limit: int


class RateLimitBackend(Protocol):
    async def increment(self, key: str, window_seconds: int) -> tuple[int, int]: ...


class RedisRateLimitBackend:
    """Atomic fixed-window counter backed by Redis."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client
        self._script = self._client.register_script(_FIXED_WINDOW_LUA)

    async def increment(self, key: str, window_seconds: int) -> tuple[int, int]:
        result = await self._script(keys=[key], args=[window_seconds])
        count, ttl = int(result[0]), int(result[1])
        return count, max(ttl, 0)


class InMemoryRateLimitBackend:
    """Process-local fallback used when Redis is unavailable."""

    def __init__(self) -> None:
        self._counters: dict[str, tuple[int, float]] = {}

    async def increment(self, key: str, window_seconds: int) -> tuple[int, int]:
        now = time.monotonic()
        count, expires_at = self._counters.get(key, (0, now + window_seconds))
        if now >= expires_at:
            count, expires_at = 0, now + window_seconds
        count += 1
        self._counters[key] = (count, expires_at)
        if len(self._counters) > 10_000:
            self._prune(now)
        return count, max(int(expires_at - now), 0)

    def _prune(self, now: float) -> None:
        expired = [k for k, (_, expires_at) in self._counters.items() if expires_at <= now]
        for key in expired:
            self._counters.pop(key, None)


class RateLimiter:
    """Enforces per-principal and per-tenant request budgets."""

    def __init__(self, backend: RateLimitBackend, *, enabled: bool = True) -> None:
        self._backend = backend
        self._enabled = enabled
        self._fallback = InMemoryRateLimitBackend()
        self._redis_healthy = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def degraded(self) -> bool:
        """True when limits are being applied from the in-process fallback."""
        return not self._redis_healthy

    async def check(
        self,
        *,
        scope: str,
        identifier: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        """Consume one unit of budget and return the decision.

        Raises:
            RateLimitedError: when the budget is exhausted.
        """
        if not self._enabled:
            return RateLimitDecision(True, limit, 0, limit)

        key = f"rl:{scope}:{identifier}"
        try:
            count, ttl = await self._backend.increment(key, window_seconds)
            if not self._redis_healthy:
                logger.info("rate_limit_backend_recovered", scope=scope)
                self._redis_healthy = True
        except Exception as exc:  # noqa: BLE001 - Redis may be unreachable
            if self._redis_healthy:
                logger.warning(
                    "rate_limit_degraded",
                    scope=scope,
                    error_type=type(exc).__name__,
                    note="falling back to in-process counters",
                )
                self._redis_healthy = False
            count, ttl = await self._fallback.increment(key, window_seconds)

        decision = RateLimitDecision(
            allowed=count <= limit,
            remaining=max(limit - count, 0),
            reset_after_seconds=ttl or window_seconds,
            limit=limit,
        )
        if not decision.allowed:
            raise RateLimitedError(
                detail=f"Rate limit exceeded for scope {scope}",
                context={
                    "scope": scope,
                    "limit": limit,
                    "window_seconds": window_seconds,
                    "reset_after_seconds": decision.reset_after_seconds,
                },
            )
        return decision


def build_rate_limiter(redis_url: str, *, enabled: bool = True) -> RateLimiter:
    client = aioredis.from_url(  # type: ignore[no-untyped-call]
        redis_url, encoding="utf-8", decode_responses=True
    )
    return RateLimiter(RedisRateLimitBackend(client), enabled=enabled)
