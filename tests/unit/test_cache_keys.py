"""Unit tests for cache key patterns and TTLs."""

from __future__ import annotations

from src.cache.cache_keys import (
    FARM_DEVICES_TTL,
    JOB_RESULT_TTL,
    JOB_STATUS_TTL,
    farm_devices_key,
    job_result_key,
    job_status_key,
)


class TestCacheKeys:
    def test_job_status_key_format(self):
        key = job_status_key("tenant-7", "abc-123")
        assert key == "app_inspector:tenant-7:job:abc-123:status"

    def test_job_status_key_is_tenant_scoped(self):
        # Same job_id under different tenants must not collide (no cross-tenant
        # cache hit bypassing RLS).
        assert job_status_key("tenant-a", "job-1") != job_status_key("tenant-b", "job-1")

    def test_job_result_key_format(self):
        key = job_result_key("tenant-7", "abc-123")
        assert key == "app_inspector:tenant-7:job:abc-123:result"

    def test_job_result_key_is_tenant_scoped(self):
        """Unreferenced today, so there is no live trigger — but the FIRST
        pre-DB cache read using this key would be a cross-tenant leak, exactly
        as ``job_status_key`` was before it was scoped. Unload the gun."""
        assert job_result_key("tenant-a", "job-1") != job_result_key("tenant-b", "job-1")

    def test_farm_devices_key_format(self):
        key = farm_devices_key("browserstack")
        assert key == "app_inspector:farm:browserstack:devices"


class TestTTLValues:
    def test_job_status_ttl(self):
        assert JOB_STATUS_TTL == 30

    def test_job_result_ttl(self):
        assert JOB_RESULT_TTL == 3600

    def test_farm_devices_ttl(self):
        assert FARM_DEVICES_TTL == 300
