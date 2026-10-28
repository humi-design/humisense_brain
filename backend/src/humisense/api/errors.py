"""Central exception handling.

Every error leaves the API in one shape, with a stable code, a safe message, and the
correlation id. Stack traces, SQL fragments, file paths, and exception text from external
systems are never returned.

Unexpected exceptions are logged in full server-side and returned to the client as a
generic internal error, so an unhandled bug cannot become an information disclosure.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from humisense.errors import (
    AuthenticationError,
    AuthorizationError,
    CsrfError,
    HumisenseError,
    RateLimitedError,
    ValidationFailure,
)
from humisense.observability.correlation import get_correlation_id

logger = structlog.get_logger(__name__)

MAX_VALIDATION_ERRORS_REPORTED = 10


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": get_correlation_id(),
        }
    }
    if details:
        body["error"]["details"] = details  # type: ignore[index]
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    """Register the application's exception handlers."""

    @app.exception_handler(HumisenseError)
    async def _humisense_error(request: Request, exc: HumisenseError) -> JSONResponse:
        log = logger.warning if exc.http_status < 500 else logger.error
        log(
            "request_error",
            error_code=exc.code,
            status_code=exc.http_status,
            path=request.url.path,
            method=request.method,
            detail=exc.detail,
            context=exc.context,
        )
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitedError):
            # Tell a well-behaved client when to try again, rather than leaving it to
            # guess and retry immediately.
            retry_after = exc.context.get("reset_after_seconds") or exc.context.get(
                "window_seconds"
            )
            if retry_after:
                headers["Retry-After"] = str(int(retry_after))
        return _error_response(
            status_code=exc.http_status,
            code=exc.code,
            message=exc.public_message,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Report field locations and messages, never the submitted values, so a rejected
        # credential is not echoed back into a response or a log.
        problems = [
            {
                "location": ".".join(str(part) for part in error.get("loc", ())),
                "message": error.get("msg", "invalid value"),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()[:MAX_VALIDATION_ERRORS_REPORTED]
        ]
        logger.info(
            "request_validation_failed",
            path=request.url.path,
            problem_count=len(exc.errors()),
        )
        failure = ValidationFailure()
        return _error_response(
            status_code=failure.http_status,
            code=failure.code,
            message=failure.public_message,
            details={"problems": problems},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {
            401: ("unauthenticated", "Authentication is required."),
            403: ("forbidden", "You are not authorized to perform this action."),
            404: ("not_found", "The requested resource was not found."),
            405: ("method_not_allowed", "That method is not allowed for this resource."),
            413: ("payload_too_large", "The request payload is too large."),
            429: ("rate_limited", "Too many requests. Try again later."),
        }
        code, message = mapping.get(
            exc.status_code, ("http_error", "The request could not be completed.")
        )
        # A 404 on an unmatched route must not echo the path, so probing cannot confirm
        # which routes exist.
        if exc.status_code >= 500:
            logger.error("http_error", status_code=exc.status_code, path=request.url.path)
        return _error_response(status_code=exc.status_code, code=code, message=message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            error_id=str(uuid.uuid4()),
        )
        return _error_response(
            status_code=500,
            code="internal_error",
            message="An internal error occurred. Quote the correlation id when reporting it.",
        )


__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "CsrfError",
    "install_exception_handlers",
]
