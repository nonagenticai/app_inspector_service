"""SQLAlchemy ORM models for app_inspector_service."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from w2v_common.database.rls import register_rls_ddl

from src.api.constants import DEFAULT_MAX_SCREENS

# ⛔ ``as_uuid=False`` is load-bearing, not a style choice. With ``as_uuid=True``
# SQLAlchemy hands back ``uuid.UUID`` objects, which silently breaks every
# ``tenant_id: str`` annotation, every ``==`` against a string tenant, every
# ``str``-keyed cache key and every JSON serialisation in this service — the shape
# that has frozen outboxes elsewhere in the fleet. ``as_uuid=False`` keeps the
# Python value a ``str`` while the COLUMN is a real Postgres ``uuid``, so the
# database gains the type check, the index gains 16-byte keys, and the RLS
# predicate can cast the GUC once (``…::uuid``) instead of casting the column on
# every row.


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""

    pass


class InspectionJob(Base):
    """Tracks a mobile app inspection job."""

    __tablename__ = "inspection_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # `index=True` makes the unique INDEX `ix_inspection_jobs_job_id`, which is
    # the only uniqueness enforcement prod has. Bare `unique=True` would also
    # emit a UniqueConstraint `inspection_jobs_job_id_key` that prod doesn't
    # have — migration 0005 drops it for fresh CI/test DBs.
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    platform: Mapped[str | None] = mapped_column(String(16))  # ios, android
    app_path: Mapped[str | None] = mapped_column(Text)
    automation_backend: Mapped[str | None] = mapped_column(String(32), default="appium")
    device_farm_provider: Mapped[str | None] = mapped_column(String(32), default="local")
    max_screens: Mapped[int | None] = mapped_column(Integer, default=DEFAULT_MAX_SCREENS)

    # Results
    screens_discovered: Mapped[int | None] = mapped_column(Integer, default=0)
    navigation_graph: Mapped[list[dict[str, str | None]] | None] = mapped_column(
        JSONB, default=list
    )
    screens_data: Mapped[list | None] = mapped_column(JSONB, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)

    # Timing
    inspection_time_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_purpose: Mapped[str | None] = mapped_column(Text)
    video_path: Mapped[str | None] = mapped_column(Text)
    enable_recording: Mapped[bool | None] = mapped_column(Boolean, default=False)
    selected_screen_ids: Mapped[list | None] = mapped_column(JSONB)
    require_selection: Mapped[bool | None] = mapped_column(Boolean, default=False)


class InspectionUsage(Base):
    """Daily usage tracking per tenant."""

    __tablename__ = "inspection_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False))
    date: Mapped[str] = mapped_column(String(10))
    # Prod schema has these nullable=YES (matches what 0001 emits). Declaring
    # them `Mapped[int | None]` keeps autogenerate quiet without flipping
    # NOT NULL on prod. Python-side `default=0` still fills every insert.
    inspections_started: Mapped[int | None] = mapped_column(Integer, default=0)
    inspections_completed: Mapped[int | None] = mapped_column(Integer, default=0)
    inspections_failed: Mapped[int | None] = mapped_column(Integer, default=0)
    total_screens_discovered: Mapped[int | None] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("tenant_id", "date", name="uq_inspection_usage_tenant_date"),
    )


# Phase D RLS: register the ENABLE + tenant_isolation policy DDL as an after_create
# event so tests' create_all applies the exact policies the 0006_rls migration ships
# to prod. Inert until the runtime connects as the restricted non-owner role with
# TENANT_RLS_MODE=enforce.
register_rls_ddl(Base.metadata)

# Phase D RLS: also create the SECURITY DEFINER cross-tenant cleanup resolvers in the
# create_all (test) path, single-sourced with the migration.
from src.database.rls_resolvers import register_resolvers  # noqa: E402

register_resolvers(Base.metadata)
