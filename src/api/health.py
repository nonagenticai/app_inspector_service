"""Health check endpoints for app_inspector_service."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import redis.asyncio as redis_lib
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.cache.redis_client import get_redis_client
from src.database.connection import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Time-based cache for the combined health check
_health_cache: dict[str, Any] = {}
_health_cache_ts: float = 0.0
_HEALTH_CACHE_TTL = 5.0  # seconds

# Upper bound on the readiness DB round trip. Must stay well under the
# kubelet readinessProbe timeoutSeconds so a hung database fails the probe
# rather than stalling it.
_READY_DB_TIMEOUT = 3.0  # seconds


@router.get(
    "/health",
    operation_id="health_check",
    summary="Combined health check for database, Redis, and storage",
    response_model=None,
)
async def health_check(request: Request) -> dict[str, Any] | JSONResponse:
    """Combined health check for all components."""
    global _health_cache, _health_cache_ts
    now = time.monotonic()
    if _health_cache and (now - _health_cache_ts) < _HEALTH_CACHE_TTL:
        return _health_cache

    try:
        db_status = "healthy"
        try:
            engine = await get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except SQLAlchemyError as e:
            db_status = f"unhealthy: {e}"

        redis_status = "healthy"
        try:
            redis_client = get_redis_client()
            if redis_client:
                await redis_client.ping()
            else:
                redis_status = "not configured"
        except redis_lib.RedisError as e:
            redis_status = f"unhealthy: {e}"

        # Real write-probe via StorageService.health_check() rather than
        # os.access, which returns True on volumes that accept the `mkdir`
        # syscall but reject `write(2)` (e.g., ConfigMap-mounted paths,
        # EFS/NFS mounts with root_squash, or volumes chowned to another uid).
        storage_status = "healthy"
        try:
            storage_service = getattr(request.app.state, "storage_service", None)
            if storage_service is None:
                storage_status = "not initialized"
            elif not await storage_service.health_check():
                storage_status = "not writable"
        except (OSError, PermissionError) as e:
            storage_status = f"unhealthy: {e}"

        overall = (
            "healthy"
            if all(s == "healthy" for s in [db_status, redis_status, storage_status])
            else "degraded"
        )

        result = {
            "status": overall,
            "components": {
                "database": db_status,
                "redis": redis_status,
                "storage": storage_status,
                "api": "healthy",
            },
        }
        _health_cache = result
        _health_cache_ts = now
        return result
    except (OSError, RuntimeError) as e:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "error": str(e)})


@router.get(
    "/health/live",
    operation_id="health_live",
    summary="Kubernetes liveness probe",
    response_model=None,
)
async def health_live() -> dict[str, str]:
    """Kubernetes liveness probe."""
    return {"status": "ok"}


@router.get("/healthz", operation_id="health.live")
async def healthz() -> dict[str, str]:
    """Modern K8s liveness probe path (alias of /health/live)."""
    return {"status": "ok"}


@router.get(
    "/health/ready",
    operation_id="health_ready",
    summary="Kubernetes readiness probe (database connectivity)",
    response_model=None,
)
async def health_ready() -> dict[str, Any] | JSONResponse:
    """Kubernetes readiness probe -- checks database connectivity.

    Readiness must be decided by a real round trip. SQLAlchemy async engines
    are lazy: obtaining the engine (or reading ``engine.pool.status()``) opens
    no connection, so an unreachable/misconfigured database would still report
    ready and kubelet would never de-route the pod. We therefore execute
    ``SELECT 1`` on a real connection, bounded by a short timeout so a hung
    database fails readiness instead of hanging the probe. Pool status is kept
    as diagnostic detail only -- it never decides ``ready``.
    """
    try:
        async with asyncio.timeout(_READY_DB_TIMEOUT):
            engine = await get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        pool = engine.pool
        pool_status = pool.status() if pool is not None else "unknown"
        return {
            "status": "healthy",
            "ready": True,
            "checks": {"database": {"status": "up", "pool": pool_status}},
        }
    except (SQLAlchemyError, TimeoutError, OSError, RuntimeError) as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "ready": False,
                "checks": {"database": {"status": "down", "error": str(e)}},
            },
        )


@router.get("/readyz", operation_id="health.ready", response_model=None)
async def readyz() -> dict[str, Any] | JSONResponse:
    """Modern K8s readiness probe path (alias of /health/ready)."""
    return await health_ready()
