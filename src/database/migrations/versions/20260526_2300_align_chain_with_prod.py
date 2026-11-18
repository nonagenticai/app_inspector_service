"""Align chain output with prod schema.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-26 23:00:00.000000

Round-9 chain reconciliation. After Round 7 stamped prod at 0004,
information_schema.columns on the live prod DB shows zero
column_default values and no `inspection_jobs_job_id_key` UniqueConstraint.
But `alembic upgrade head` against an empty DB produces a schema with
server_defaults on 15 columns, two NOT-NULL booleans, and the redundant
UniqueConstraint (prod was bootstrapped by `Base.metadata.create_all` at
a time when the model declarations differed from what 0001 now emits).

The drift is exclusively in the direction of "chain output has more
than prod has", so this migration removes the surplus with idempotent
raw SQL. On prod: every statement is a no-op. On fresh CI / test-namespace
DBs (which just had 0001-0004 applied): brings the schema to prod-parity
so test environments and prod behave identically against future migrations.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INSPECTION_JOBS_DEFAULT_COLS = (
    "status",
    "automation_backend",
    "device_farm_provider",
    "max_screens",
    "screens_discovered",
    "navigation_graph",
    "screens_data",
    "created_at",
    "updated_at",
    "enable_recording",
    "require_selection",
)
_INSPECTION_USAGE_DEFAULT_COLS = (
    "inspections_started",
    "inspections_completed",
    "inspections_failed",
    "total_screens_discovered",
)


def upgrade() -> None:
    # Drop redundant UniqueConstraint that prod never had — the unique INDEX
    # `ix_inspection_jobs_job_id` already enforces the same invariant.
    op.execute("ALTER TABLE inspection_jobs DROP CONSTRAINT IF EXISTS inspection_jobs_job_id_key")

    # Drop DDL-level defaults; prod has none, Python-side `default=` in
    # models.py continues to fill values on insert.
    for col in _INSPECTION_JOBS_DEFAULT_COLS:
        op.execute(f"ALTER TABLE inspection_jobs ALTER COLUMN {col} DROP DEFAULT")
    for col in _INSPECTION_USAGE_DEFAULT_COLS:
        op.execute(f"ALTER TABLE inspection_usage ALTER COLUMN {col} DROP DEFAULT")

    # Relax NOT NULL on the two booleans — prod has them nullable, chain
    # produced NOT NULL from 0004's `nullable=False`.
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN enable_recording DROP NOT NULL")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN require_selection DROP NOT NULL")


def downgrade() -> None:
    # Reinstate what 0001/0004 originally created. Idempotent on a DB that
    # never had this revision applied — but if downgrade is being run against
    # prod (which was bootstrapped without these defaults), the SET DEFAULT
    # statements will reintroduce DDL state that prod has never had. That is
    # intentional: downgrade returns to the chain's prior declared shape,
    # not to "what prod happened to look like when stamped".
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN status SET DEFAULT 'pending'")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN automation_backend SET DEFAULT 'appium'")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN device_farm_provider SET DEFAULT 'local'")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN max_screens SET DEFAULT '20'")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN screens_discovered SET DEFAULT '0'")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN navigation_graph SET DEFAULT '{}'")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN screens_data SET DEFAULT '[]'")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN updated_at SET DEFAULT now()")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN enable_recording SET DEFAULT false")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN require_selection SET DEFAULT false")
    for col in _INSPECTION_USAGE_DEFAULT_COLS:
        op.execute(f"ALTER TABLE inspection_usage ALTER COLUMN {col} SET DEFAULT '0'")

    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN enable_recording SET NOT NULL")
    op.execute("ALTER TABLE inspection_jobs ALTER COLUMN require_selection SET NOT NULL")

    op.execute(
        "ALTER TABLE inspection_jobs ADD CONSTRAINT inspection_jobs_job_id_key UNIQUE (job_id)"
    )
