"""Tests for configuration settings.

Validates defaults, environment overrides, validation constraints,
and the actual nested structure of the Settings object.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.api.constants import RETENTION_DAYS, STUCK_JOB_TIMEOUT_MINUTES
from src.config import (
    AcquisitionSettings,
    AnalysisSettings,
    APISettings,
    CleanupSettings,
    DatabaseSettings,
    LoggingSettings,
    RateLimitSettings,
    Settings,
    StorageSettings,
    clear_settings_cache,
)


class TestDatabaseSettings:
    def test_defaults(self):
        # No url= is passed on purpose: the field is populated from the
        # DATABASE_URL env var at runtime (seeded by the conftest module-level
        # setdefault and the autouse fixture).
        settings = DatabaseSettings()
        assert "postgresql" in settings.url
        assert settings.pool_size == 10
        assert settings.echo is False

    def test_env_override(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://custom:5432/db"}):
            settings = DatabaseSettings()  # url comes from the patched env
            assert settings.url == "postgresql+psycopg://custom:5432/db"


class TestAPISettings:
    def test_defaults(self):
        settings = APISettings()
        assert settings.port == 6311
        assert settings.debug is False

    def test_cors_origins_from_json_env(self):
        """Env vars for list fields must be JSON-encoded (pydantic-settings 2.x)."""
        with patch.dict(
            os.environ,
            {"API_CORS_ORIGINS": '["http://localhost:3000","http://localhost:8080"]'},
        ):
            settings = APISettings()
            assert len(settings.cors_origins) == 2
            assert "http://localhost:3000" in settings.cors_origins
            assert "http://localhost:8080" in settings.cors_origins

    def test_cors_origins_empty(self):
        with patch.dict(os.environ, {"API_CORS_ORIGINS": "[]"}):
            settings = APISettings()
            assert settings.cors_origins == []


class TestLoggingSettings:
    def test_valid_level_uppercased(self):
        """LoggingSettings should normalize level to uppercase."""
        settings = LoggingSettings(level="debug")
        assert settings.level == "DEBUG"

    def test_valid_levels_accepted(self):
        """All standard Python logging levels should be accepted."""
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            settings = LoggingSettings(level=level)
            assert settings.level == level

    def test_invalid_level_raises_validation_error(self):
        """Invalid logging level should raise ValidationError, not a bare Exception."""
        with pytest.raises(ValidationError) as exc_info:
            LoggingSettings(level="INVALID")
        assert "level" in str(exc_info.value).lower()


class TestRateLimitSettings:
    def test_defaults(self):
        settings = RateLimitSettings()
        assert settings.requests_per_minute == 30
        assert settings.concurrent_jobs_per_tenant == 5

    def test_env_override(self):
        with patch.dict(os.environ, {"RATE_LIMIT_REQUESTS_PER_MINUTE": "100"}):
            settings = RateLimitSettings()
            assert settings.requests_per_minute == 100


class TestCleanupSettings:
    def test_defaults_match_constants(self):
        """Default values should match the named constants."""
        settings = CleanupSettings()
        assert settings.retention_days == RETENTION_DAYS
        assert settings.stuck_job_timeout_minutes == STUCK_JOB_TIMEOUT_MINUTES

    def test_env_override(self):
        with patch.dict(os.environ, {"CLEANUP_RETENTION_DAYS": "7"}):
            settings = CleanupSettings()
            assert settings.retention_days == 7

    def test_stuck_timeout_env_override(self):
        with patch.dict(os.environ, {"CLEANUP_STUCK_JOB_TIMEOUT_MINUTES": "120"}):
            settings = CleanupSettings()
            assert settings.stuck_job_timeout_minutes == 120


class TestAnalysisSettings:
    def test_defaults(self):
        settings = AnalysisSettings()
        assert settings.enable_purpose_inference is True
        assert settings.purpose_inference_mode == "heuristic"

    def test_env_override_disables_purpose_inference(self):
        with patch.dict(os.environ, {"ANALYSIS_ENABLE_PURPOSE_INFERENCE": "false"}):
            settings = AnalysisSettings()
            assert settings.enable_purpose_inference is False


class TestAcquisitionSettings:
    def test_defaults(self):
        settings = AcquisitionSettings()
        assert settings.download_timeout == 300
        assert settings.max_file_size_mb == 500
        assert settings.temp_dir == "/tmp/app_inspector"
        assert isinstance(settings.allowed_local_paths, list)
        assert len(settings.allowed_local_paths) > 0

    def test_env_override(self):
        with patch.dict(os.environ, {"ACQUISITION_DOWNLOAD_TIMEOUT": "60"}):
            settings = AcquisitionSettings()
            assert settings.download_timeout == 60


class TestStorageSettings:
    def test_minio_bucket_default(self):
        # minio_endpoint is populated from STORAGE_MINIO_ENDPOINT env at runtime.
        settings = StorageSettings()
        assert settings.minio_bucket == "app-inspector-service"


class TestSettings:
    def test_combined_settings_has_all_subsections(self):
        """Settings should have all nested config sections properly initialized."""
        clear_settings_cache()
        settings = Settings()

        assert settings.app_name == "App Inspector Service"
        assert settings.app_version == "0.1.0"
        # environment is "testing" in test conftest, "development" by default
        assert isinstance(settings.environment, str)
        assert len(settings.environment) > 0

        # Each subsection should be the correct type with proper defaults
        assert isinstance(settings.database, DatabaseSettings)
        assert isinstance(settings.redis, type(settings.redis))  # RedisSettings from w2v_common
        assert isinstance(settings.api, APISettings)
        assert settings.api.port == 6311
        assert isinstance(settings.cleanup, CleanupSettings)
        assert settings.cleanup.retention_days == RETENTION_DAYS
        assert isinstance(settings.analysis, AnalysisSettings)
        assert settings.analysis.enable_purpose_inference is True
        assert isinstance(settings.rate_limit, RateLimitSettings)
        assert settings.rate_limit.requests_per_minute == 30
        assert isinstance(settings.acquisition, AcquisitionSettings)
        assert not hasattr(settings, "device_farm")  # dead section deleted (w2v P5)
        assert isinstance(settings.storage, StorageSettings)

        clear_settings_cache()

    def test_is_development(self):
        settings = Settings(environment="development")
        assert settings.is_development() is True
        assert settings.is_production() is False

    def test_is_production(self):
        settings = Settings(environment="production")
        assert settings.is_production() is True
        assert settings.is_development() is False

    def test_case_insensitive_environment(self):
        """Environment check should be case-insensitive."""
        settings = Settings(environment="Production")
        assert settings.is_production() is True

        settings = Settings(environment="DEVELOPMENT")
        assert settings.is_development() is True

    def test_get_database_url_returns_database_url(self):
        """get_database_url() should return the actual database URL string."""
        settings = Settings()
        url = settings.get_database_url()
        assert isinstance(url, str)
        assert "postgresql" in url

    def test_get_redis_url_returns_redis_url(self):
        """get_redis_url() should return the actual Redis URL string."""
        settings = Settings()
        url = settings.get_redis_url()
        assert isinstance(url, str)
        assert "redis" in url
