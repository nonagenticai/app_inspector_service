"""Add video_path and enable_recording columns to inspection_jobs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-02-23 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inspection_jobs",
        sa.Column("video_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "inspection_jobs",
        sa.Column("enable_recording", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("inspection_jobs", "enable_recording")
    op.drop_column("inspection_jobs", "video_path")
