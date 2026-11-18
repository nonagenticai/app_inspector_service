"""
Configuration management for app_inspector_service using Pydantic Settings.

Hierarchical settings with sub-configs.
Follows skill_builder/tts_service config pattern.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from w2v_common.config import APISettings as _BaseAPISettings
from w2v_common.config import AuthSettings, DatabaseSettings, LoggingSettings, RedisSettings
from w2v_common.config import StorageSettings as _BaseStorageSettings

__all__ = [
    "AuthSettings",
    "DatabaseSettings",
    "LoggingSettings",
    "RedisSettings",
]


class APISettings(_BaseAPISettings):
    """App inspector service API settings (port 6311)."""

    port: int = Field(default=6311, description="API server port")


class StorageSettings(_BaseStorageSettings):
    """File storage configuration."""

    minio_bucket: str = Field(default="app-inspector-service", description="MinIO bucket name")


class AcquisitionSettings(BaseSettings):
    """App acquisition configuration."""

    download_timeout: int = Field(default=300, description="Download timeout in seconds")
    max_file_size_mb: int = Field(default=500, description="Max app file size in MB")
    temp_dir: str = Field(
        default="/tmp/app_inspector",  # noqa: S108  # nosec B108 -- configurable default for containerized service
        description="Temp directory for downloads",
    )
    allowed_local_paths: list[str] = Field(
        default_factory=lambda: ["/app/data", "/tmp/app_inspector"],  # noqa: S108  # nosec B108 -- configurable default for containerized service
        description="Allowed base directories for local file paths",
    )
    allowed_minio_bucket: str = Field(
        default="",
        description=(
            "The ONE MinIO bucket a minio:// app_path may name. Empty falls back to "
            "STORAGE_MINIO_BUCKET. w2v_common's StorageService has no tenant concept, so "
            "bucket + tenant-prefix confinement is enforced here (see "
            "src.services.app_acquisition.confine_minio_uri). NOTE: minio:// acquisition "
            "is not wired up (no minio_storage is ever passed to AppAcquisitionService) "
            "and no producer writes objects under a <tenant_id>/ prefix in this bucket, "
            "so the accept-set is currently EMPTY by design — enabling it needs an "
            "upload producer that uses <tenant_id>/ keys."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="ACQUISITION_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class AnalysisSettings(BaseSettings):
    """Analysis configuration."""

    enable_purpose_inference: bool = Field(default=True, description="Enable purpose inference")
    purpose_inference_mode: str = Field(
        default="heuristic",
        description="Purpose inference mode: heuristic or llm",
    )

    model_config = SettingsConfigDict(
        env_prefix="ANALYSIS_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class RateLimitSettings(BaseSettings):
    """Rate limiting configuration."""

    requests_per_minute: int = Field(
        default=30, description="Max inspection requests per minute per tenant"
    )
    concurrent_jobs_per_tenant: int = Field(
        default=5, description="Max concurrent inspection jobs per tenant"
    )

    model_config = SettingsConfigDict(
        env_prefix="RATE_LIMIT_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class CleanupSettings(BaseSettings):
    """Job cleanup configuration."""

    retention_days: int = Field(default=30, description="Days to retain completed/failed jobs")
    stuck_job_timeout_minutes: int = Field(
        default=60, description="Minutes after which a processing job is considered stuck"
    )

    model_config = SettingsConfigDict(
        env_prefix="CLEANUP_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class Settings(BaseSettings):
    """Main application settings combining all configuration sections."""

    app_name: str = Field(default="App Inspector Service", description="Application name")
    app_version: str = Field(default="0.1.0", description="Application version")
    environment: str = Field(
        default="development", description="Environment (development, staging, production)"
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    api: APISettings = Field(default_factory=APISettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    acquisition: AcquisitionSettings = Field(default_factory=AcquisitionSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    cleanup: CleanupSettings = Field(default_factory=CleanupSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    def get_database_url(self) -> str:
        """Return the configured database connection URL."""
        return self.database.url

    def get_redis_url(self) -> str:
        """Return the configured Redis connection URL."""
        return self.redis.url

    def is_production(self) -> bool:
        """Return True if the environment is production."""
        return self.environment.lower() == "production"

    def is_development(self) -> bool:
        """Return True if the environment is development."""
        return self.environment.lower() == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear the cached settings instance (useful for testing)."""
    get_settings.cache_clear()
