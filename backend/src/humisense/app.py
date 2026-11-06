"""FastAPI application factory.

Production behaviour is decided by configuration, not by a code branch:

* Interactive API documentation is enabled in development and test and disabled in
  production unless explicitly authorized.
* CORS allows only the configured origins, with credentials, and never the wildcard.
* Security headers, correlation ids, body-size limits, and request logging are applied to
  every response.
* The application refuses to start with an insecure production configuration, because
  :func:`build_container` calls ``Settings.validate_for_production``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from humisense import __version__
from humisense.api.errors import install_exception_handlers
from humisense.api.middleware import (
    BodySizeLimitMiddleware,
    CorrelationMiddleware,
    RequestLogMiddleware,
    SecurityHeadersMiddleware,
)
from humisense.api.routers import (
    analytics,
    auth,
    connections,
    connectors,
    health,
    ingestion,
    metrics,
    work,
)
from humisense.config import Settings, get_settings
from humisense.container import Container, build_container
from humisense.logging_setup import configure_logging

logger = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    """Build the FastAPI application."""
    resolved_settings = settings or get_settings()
    configure_logging(json_logs=resolved_settings.log_json, level=resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_container = container or build_container(resolved_settings)
        app.state.container = active_container
        logger.info(
            "application_started",
            version=__version__,
            environment=resolved_settings.app_env,
            auth_mode=resolved_settings.auth_mode,
            ingestion_sink=active_container.ingestion.sink_name,
            docs_enabled=resolved_settings.docs_public,
        )
        try:
            yield
        finally:
            if container is None:
                await active_container.close()
            logger.info("application_stopped")

    docs_url = "/docs" if resolved_settings.docs_public else None
    openapi_url = "/openapi.json" if resolved_settings.docs_public else None

    app = FastAPI(
        title="Humisense Brain / OS — Layer 1",
        description=(
            "Secure connector and sensory platform. Captures authorized source records "
            "and transports them to Layer 2. No reasoning or agent logic runs here."
        ),
        version=__version__,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
        lifespan=lifespan,
        # Errors are shaped by the handlers below, so the default response model is
        # suppressed rather than documenting a shape the API does not return.
        responses={},
    )

    # Middleware is applied outermost-first. Correlation runs first so the error handler
    # and access log can reference it; the body limit runs before routing.
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, production=resolved_settings.is_production)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=resolved_settings.max_request_body_bytes)
    app.add_middleware(CorrelationMiddleware)

    if resolved_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token", "X-Correlation-Id"],
            expose_headers=["X-Correlation-Id"],
            max_age=600,
        )

    install_exception_handlers(app)

    prefix = resolved_settings.api_prefix
    app.include_router(health.router, prefix=prefix)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(connectors.router, prefix=prefix)
    app.include_router(connections.router, prefix=prefix)
    app.include_router(ingestion.router, prefix=prefix)
    app.include_router(analytics.router, prefix=prefix)
    app.include_router(work.router, prefix=prefix)
    if resolved_settings.layer2_ingest_enabled:
        # Registering the machine receiver only when it is enabled means a disabled
        # deployment has no route to reach it, rather than a route that exists and refuses.
        # The human-facing quarantine endpoints stay available either way.
        app.include_router(ingestion.machine_router, prefix=prefix)
    # The scrape endpoint is registered only when metrics are enabled, so a disabled
    # deployment has no route to reach it at all.
    if resolved_settings.metrics_enabled:
        app.include_router(metrics.router, prefix=prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        """Return a minimal, non-informative service banner."""
        return {"service": "humisense-brain", "layer": 2, "version": __version__}

    return app
