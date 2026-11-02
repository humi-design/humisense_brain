"""Cross-cutting middleware: correlation ids, body limits, security headers, logging.

Order matters. Correlation is established first so every later layer, including the error
handler, can reference it. The body limit runs before routing so an oversized payload is
rejected before it is parsed.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from humisense.errors import PayloadTooLargeError
from humisense.observability.correlation import (
    CORRELATION_HEADER,
    get_correlation_id,
    sanitize_correlation_id,
    set_correlation_id,
)
from humisense.security.headers import security_headers

logger = structlog.get_logger(__name__)

UNPROTECTED_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/health/ready",
        "/api/v1/auth/session",
        "/api/v1/auth/logout",
    }
)


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Attach a server-validated correlation id to every request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(CORRELATION_HEADER)
        correlation_id = set_correlation_id(incoming or "")
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("correlation_id")
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared or actual body exceeds the configured limit."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    return self._too_large(request)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "invalid_request",
                            "message": "The Content-Length header is not a number.",
                            "correlation_id": get_correlation_id(),
                        }
                    },
                )
        return await call_next(request)

    def _too_large(self, request: Request) -> JSONResponse:
        error = PayloadTooLargeError()
        logger.warning(
            "request_body_too_large",
            path=request.url.path,
            max_bytes=self._max_bytes,
        )
        return JSONResponse(
            status_code=error.http_status,
            content={
                "error": {
                    "code": error.code,
                    "message": error.public_message,
                    "correlation_id": get_correlation_id(),
                }
            },
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply secure response headers to every response."""

    def __init__(self, app: ASGIApp, *, production: bool) -> None:
        super().__init__(app)
        self._headers = security_headers(production=production)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in self._headers.items():
            response.headers.setdefault(name, value)
        return response


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured access log line per request.

    The query string is not logged, because a filter or search parameter can carry source
    data. The path is logged, and paths never contain record content.
    """

    def __init__(self, app: ASGIApp, *, metrics: object | None = None) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.monotonic() - started
            logger.error(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration * 1000, 2),
            )
            raise
        duration = time.monotonic() - started
        route = _route_template(request)
        principal = getattr(request.state, "principal", None)
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            route=route,
            status_code=response.status_code,
            duration_ms=round(duration * 1000, 2),
            organization_id=str(principal.organization_id) if principal else None,
            user_id=str(principal.user_id) if principal else None,
        )
        if self._metrics is not None:
            self._metrics.api_request(  # type: ignore[attr-defined]
                route=route,
                method=request.method,
                status_code=response.status_code,
                duration=duration,
            )
        return response


def _route_template(request: Request) -> str:
    """Return the route template, so metrics do not gain a label per object id."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def correlation_id_from(request: Request) -> str:
    return sanitize_correlation_id(request.headers.get(CORRELATION_HEADER))
