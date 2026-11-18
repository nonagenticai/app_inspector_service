"""ASGI application for app_inspector_service.

FastAPI REST API following the canonical w2v fleet pattern
(video_analyzer_service / analysis_service / nlp_service).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

import redis.asyncio as redis_lib
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from w2v_common.asgi import register_error_envelope_handlers, register_middleware
from w2v_common.observability import database_endpoint, redis_endpoint

from src.cache.redis_client import close_redis, get_redis_client, init_redis
from src.config import get_settings
from src.database.connection import close_engine, get_engine
from src.utils.logging_config import clamp_credential_leaking_loggers

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# LOG_LEVEL=DEBUG must not undo the Appium hub-URL redaction in
# src/services/appium_client.py: selenium re-logs the same credentialed URL one
# level down. Clamp immediately after basicConfig, before anything can connect.
clamp_credential_leaking_loggers()

logger = logging.getLogger(__name__)

settings = get_settings()


# ============================================================================
# Health Check Logging Filter
# ============================================================================


class HealthCheckLoggingFilter(logging.Filter):
    """Suppress noisy health-check endpoint log lines from uvicorn access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for health-check paths so they are not logged."""
        message = record.getMessage()
        return not any(
            path in message
            for path in ["/health", "/health/ready", "/health/live", "/readyz", "/healthz"]
        )


uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addFilter(HealthCheckLoggingFilter())


# ============================================================================
# Lifespan
# ============================================================================


async def _init_database(app: FastAPI) -> None:
    """Connect to the database and ensure schema is created."""
    # Phase D RLS: install the after_begin GUC re-bind listener before any
    # session is opened, so the multi-commit background pipeline keeps
    # app.current_tenant bound across each commit under enforce.
    from src.database.rls_events import install_rls_tenant_rebind

    install_rls_tenant_rebind()
    try:
        db_engine = await get_engine()
        app.state.db_engine = db_engine
        logger.info("Database connected: %s", database_endpoint(settings.database.url))
    except (OSError, SQLAlchemyError) as e:
        logger.error("Failed to connect to database: %s", e, exc_info=True)
        raise

    # Schema is owned by alembic -- the gitops Deployment runs
    # ``alembic upgrade head`` in an initContainer before this app
    # starts (see gitops-production
    # clusters/strato/services/w2v/deployments.yaml). Do NOT call
    # ``Base.metadata.create_all`` here: it bypasses migration history
    # and can drift the live schema away from what alembic expects.
    logger.info("Database connected (schema managed by alembic)")


async def _init_redis(app: FastAPI) -> None:
    """Connect to Redis; failure is non-fatal."""
    try:
        redis_client = await init_redis()
        await redis_client.ping()
        app.state.redis_client = get_redis_client()
        logger.info("Redis connected: %s", redis_endpoint(settings.redis.url))
    except (OSError, redis_lib.RedisError) as e:
        logger.error("Failed to connect to Redis: %s", e, exc_info=True)
        logger.warning("Continuing without Redis -- caching will be disabled")


async def _init_services(app: FastAPI) -> None:
    """Initialize storage and inspector services."""
    from src.services.inspector_service import InspectorService
    from src.services.storage_service import StorageService

    storage = StorageService()
    await storage.initialize()
    app.state.storage_service = storage
    app.state.inspector_service = InspectorService(storage=storage)


async def _run_cleanup_loop() -> None:
    """Periodically clean up stuck and old jobs."""
    from src.database.session import get_db_session
    from src.services.cleanup_service import CleanupService

    cleanup_service = CleanupService()
    while True:
        try:
            await asyncio.sleep(300)  # Run every 5 minutes
            # Single-flight across replicas: run_sweep guards the prune passes
            # with a cross-replica advisory lock so only one pod sweeps per tick.
            async with get_db_session() as db:
                result = await cleanup_service.run_sweep(
                    db,
                    stuck_timeout_minutes=settings.cleanup.stuck_job_timeout_minutes,
                    retention_days=settings.cleanup.retention_days,
                )
            if result is None:
                logger.debug("Cleanup tick swept by another replica")
        except asyncio.CancelledError:
            break
        except (OSError, SQLAlchemyError) as e:
            logger.error("Cleanup task error: %s", e, exc_info=True)


async def _shutdown(cleanup_task: asyncio.Task[None]) -> None:
    """Cancel the cleanup task and close DB/Redis connections."""
    logger.info("Shutting down App Inspector Service")
    cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup_task
    logger.info("Cleanup background task stopped")
    try:
        await close_redis()
    except (OSError, redis_lib.RedisError) as e:
        logger.error("Error closing Redis: %s", e, exc_info=True)
    try:
        await close_engine()
    except (OSError, SQLAlchemyError) as e:
        logger.error("Error closing database: %s", e, exc_info=True)
    logger.info("App Inspector Service shutdown complete")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan -- init DB, Redis."""
    logger.info("=" * 70)
    logger.info("Starting App Inspector Service")
    logger.info("=" * 70)

    await _init_database(app)
    await _init_redis(app)
    await _init_services(app)

    cleanup_task = asyncio.create_task(_run_cleanup_loop())
    logger.info("Cleanup background task started")

    logger.info("=" * 70)
    logger.info("App Inspector Service started successfully")
    logger.info("Environment: %s", settings.environment)
    logger.info("API: %s:%d", settings.api.host, settings.api.port)
    logger.info("=" * 70)

    yield

    await _shutdown(cleanup_task)


# ============================================================================
# Public-endpoint policy (kept in sync with w2v_common.auth.middleware)
# ============================================================================
# Used only by ``custom_openapi()`` below to decide which paths advertise the
# Bearer security requirement in the schema. The actual auth enforcement is
# done by ``w2v_common.auth.middleware.AuthMiddleware``.
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        "/health/live",
        "/health/ready",
        "/healthz",
        "/readyz",
        "/ready",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
        "/metrics",
    }
)
_PUBLIC_PREFIXES: tuple[str, ...] = ("/docs", "/redoc", "/health/")


# ============================================================================
# Create App
# ============================================================================

app = FastAPI(
    title=settings.app_name,
    description="Mobile app inspection microservice with Appium and device farm integration",
    version=settings.app_version,
    lifespan=lifespan,
    redirect_slashes=False,
    swagger_ui_parameters={"persistAuthorization": True},
    # Fallback unique-id generator. Every route still sets ``operation_id=``
    # explicitly per ``OPERATION_ID_PLAN.md``; this exists so a future route
    # that forgets the kwarg degrades to ``<tag>_<func>`` instead of FastAPI's
    # default ``<func>_<path>_<method>`` (garbage in generated clients).
    generate_unique_id_function=lambda route: (
        f"{route.tags[0] if route.tags else 'default'}_{route.name}"
    ),
    openapi_tags=[
        {"name": "health", "description": "Liveness, readiness, dependency probes."},
        {
            "name": "inspect",
            "description": (
                "Mobile app inspection jobs: submission, status, results, "
                "screen selection, comparison, export, and recording playback."
            ),
        },
    ],
)

# CORS
register_middleware(
    app,
    CORSMiddleware,
    allow_origins=settings.api.cors_origins,
    allow_credentials=settings.api.cors_allow_credentials,
    allow_methods=settings.api.cors_allow_methods,
    allow_headers=settings.api.cors_allow_headers,
)


# Rate limiter middleware
from src.middleware.rate_limiter import (  # noqa: E402 -- must import after app initialization
    RateLimitMiddleware,
)

register_middleware(app, RateLimitMiddleware)
logger.info("Rate limiter middleware enabled")

# Auth middleware -- delegated to w2v_common.auth.middleware via src.auth.middleware
from src.auth.middleware import AuthMiddleware  # noqa: E402 -- must import after app initialization

if settings.auth.enabled:
    register_middleware(app, AuthMiddleware)
    logger.info("Authentication middleware enabled")
else:
    logger.warning("Running WITHOUT authentication -- development mode only!")


# Request ID middleware — registered last so it is the OUTERMOST layer and runs first
# inbound, binding the request_id ContextVar before auth/handlers execute.
from w2v_common.observability.request_context import RequestIdLogFilter  # noqa: E402

from src.middleware.request_id import RequestIdMiddleware  # noqa: E402

register_middleware(app, RequestIdMiddleware)
logging.getLogger().addFilter(RequestIdLogFilter())


# ============================================================================
# Root & Health Endpoints
# ============================================================================


@app.get(
    "/",
    operation_id="service_root",
    tags=["health"],
    summary="Service banner + endpoint index",
)
async def root() -> dict[str, Any]:
    """Return service metadata and available endpoints."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "description": "Mobile app inspection microservice with Appium and device farm integration",
        "endpoints": {
            "health": "/health",
            "docs": "/docs",
            "inspect": "/api/inspect",
            "compare": "/api/inspect/compare",
            "farms": "/api/inspect/farms",
        },
        "status": "running",
        "environment": settings.environment,
    }


# Mount routers
from src.api.health import (  # noqa: E402 -- must import after app initialization
    router as health_router,
)
from src.api.routes import (  # noqa: E402 -- must import after app initialization
    router as api_router,
)

app.include_router(health_router)
app.include_router(api_router)


# ============================================================================
# OpenAPI customisation -- BearerAuth scheme + TenantIdHeader parameter
# ============================================================================
#
# Mirrors the kag / worker_management / nlp_service / analysis_service /
# video_compiler_service / video_analyzer_service pattern: every protected
# operation advertises ``security: [{BearerAuth: []}]`` and a reusable
# ``TenantIdHeader`` parameter so Swagger UI exposes the license-token
# transport correctly. The actual enforcement lives in
# ``w2v_common.auth.middleware.AuthMiddleware``.
def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )

    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "License",
        "description": (
            "License token. Transport: `X-Tenant-Id: Bearer <token>` "
            "(not the Authorization header -- that is reserved for caller-side auth)."
        ),
    }
    components.setdefault("parameters", {})["TenantIdHeader"] = {
        "name": "X-Tenant-Id",
        "in": "header",
        "required": False,
        "description": "License token transport: `Bearer <token>`.",
        "schema": {"type": "string"},
    }

    for path, item in schema.get("paths", {}).items():
        clean = path.rstrip("/") or "/"
        is_public = clean in _PUBLIC_PATHS or any(clean.startswith(p) for p in _PUBLIC_PREFIXES)
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            if is_public:
                operation.pop("security", None)
            else:
                operation.setdefault("security", [{"BearerAuth": []}])
            params = operation.setdefault("parameters", [])
            if not any(
                p.get("name", "").lower() == "x-tenant-id" and p.get("in") == "header"
                for p in params
            ):
                params.append({"$ref": "#/components/parameters/TenantIdHeader"})

    app.openapi_schema = schema
    return app.openapi_schema


object.__setattr__(app, "openapi", custom_openapi)


# ============================================================================
# Error Handlers — canonical fleet error envelope (shape #2), from w2v_common
# ============================================================================
register_error_envelope_handlers(app, debug=settings.api.debug)


# ============================================================================
# Main Entry Point
# ============================================================================


def main() -> None:
    """Main entry point for running the ASGI application."""
    import uvicorn

    uvicorn.run(
        "src.asgi:app",
        host=settings.api.host,
        port=settings.api.port,
        log_level=settings.logging.level.lower(),
        reload=settings.api.reload,
    )


if __name__ == "__main__":
    main()
