"""Centralized Redis cache key patterns and TTLs for app_inspector_service."""

from __future__ import annotations

# TTLs in seconds
JOB_STATUS_TTL = 30  # Short TTL, status changes frequently
JOB_RESULT_TTL = 3600  # 1 hour for completed results
FARM_DEVICES_TTL = 300  # 5 minutes for device listings


def job_status_key(tenant_id: str, job_id: str) -> str:
    """Cache key for job status, scoped by tenant.

    The status cache is read *before* the RLS-scoped DB lookup, so the key
    must include the tenant — otherwise a cache hit would return another
    tenant's job status without any ownership check, bypassing RLS.
    """
    return f"app_inspector:{tenant_id}:job:{job_id}:status"


def job_result_key(tenant_id: str, job_id: str) -> str:
    """Cache key for a completed job result, scoped by tenant.

    Tenant-scoped for the same reason as :func:`job_status_key`. This key has
    NO caller in ``src/`` today, so there was no live leak — but an unscoped
    key is a loaded gun: the first pre-DB cache read added against it would
    serve one tenant's full inspection result to another, with no ownership
    check and RLS bypassed entirely. Scoping it now costs nothing and makes
    that mistake unreachable.
    """
    return f"app_inspector:{tenant_id}:job:{job_id}:result"


def farm_devices_key(provider: str) -> str:
    """Cache key for device farm device listing."""
    return f"app_inspector:farm:{provider}:devices"
