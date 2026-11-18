"""Initial schema for app_inspector_service.

Revision ID: 0001
Revises:
Create Date: 2026-02-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inspection_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("platform", sa.String(16), nullable=True),
        sa.Column("app_path", sa.Text(), nullable=True),
        sa.Column(
            "automation_backend",
            sa.String(32),
            server_default="appium",
            nullable=True,
        ),
        sa.Column(
            "device_farm_provider",
            sa.String(32),
            server_default="local",
            nullable=True,
        ),
        sa.Column("max_screens", sa.Integer(), server_default="20", nullable=True),
        sa.Column(
            "screens_discovered",
            sa.Integer(),
            server_default="0",
            nullable=True,
        ),
        sa.Column(
            "navigation_graph",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=True,
        ),
        sa.Column(
            "screens_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("inspection_time_seconds", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        op.f("ix_inspection_jobs_job_id"),
        "inspection_jobs",
        ["job_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_inspection_jobs_tenant_id"),
        "inspection_jobs",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "inspection_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("date", sa.String(10), nullable=False),
        sa.Column(
            "inspections_started",
            sa.Integer(),
            server_default="0",
            nullable=True,
        ),
        sa.Column(
            "inspections_completed",
            sa.Integer(),
            server_default="0",
            nullable=True,
        ),
        sa.Column(
            "inspections_failed",
            sa.Integer(),
            server_default="0",
            nullable=True,
        ),
        sa.Column(
            "total_screens_discovered",
            sa.Integer(),
            server_default="0",
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "date", name="uq_inspection_usage_tenant_date"),
    )


def downgrade() -> None:
    op.drop_table("inspection_usage")
    op.drop_index(op.f("ix_inspection_jobs_tenant_id"), table_name="inspection_jobs")
    op.drop_index(op.f("ix_inspection_jobs_job_id"), table_name="inspection_jobs")
    op.drop_table("inspection_jobs")
