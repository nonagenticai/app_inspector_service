"""Stuck-job sweep must also reclaim rows stranded in ``pending``.

``start_inspection`` COMMITs the row as ``pending`` and only THEN spawns
``_process_inspection_bg``; ``JobManager.mark_processing`` is that task's first DB
write. Anything that kills the task inside that window — a rolling deploy / SIGTERM
right after the 202, or ``asyncio.CancelledError`` generally (a ``BaseException``,
so the task's ``except Exception -> mark_failed`` never runs) — leaves the row
``pending``.

``prune_stuck_inspection_jobs`` matched only ``status = 'processing'`` and
``prune_old_inspection_jobs`` deletes only ``completed``/``failed``, so such a row
was neither reclaimed NOR retained-out: a client polling
``GET /api/inspect/{job_id}`` saw ``pending`` for the life of the database.

A genuinely pending job is pending for milliseconds, so the existing
``STUCK_JOB_TIMEOUT_MINUTES`` (60) cutoff cannot race a healthy one.

Revision ID: 0008_prune_pending
Revises: 0007_tenant_uuid
"""

from alembic import op

revision: str = "0008_prune_pending"
down_revision: str | None = "0007_tenant_uuid"
branch_labels = None
depends_on = None


# Pinned literals: a migration is a historical record and must not move when
# ``src/database/rls_resolvers.py`` moves.
_PRUNE_STUCK_WITH_PENDING = """
CREATE OR REPLACE FUNCTION prune_stuck_inspection_jobs(p_cutoff timestamptz)
  RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    WITH u AS (
      UPDATE inspection_jobs
      SET status = 'failed',
          error_message = 'Job timed out (stuck-job cleanup)',
          completed_at = now(),
          updated_at = now()
      WHERE status IN ('pending', 'processing') AND updated_at < p_cutoff
      RETURNING 1
    ) SELECT count(*)::int FROM u;
  $fn$
"""

_PRUNE_STUCK_PROCESSING_ONLY = """
CREATE OR REPLACE FUNCTION prune_stuck_inspection_jobs(p_cutoff timestamptz)
  RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    WITH u AS (
      UPDATE inspection_jobs
      SET status = 'failed',
          error_message = 'Job timed out (stuck-job cleanup)',
          completed_at = now(),
          updated_at = now()
      WHERE status = 'processing' AND updated_at < p_cutoff
      RETURNING 1
    ) SELECT count(*)::int FROM u;
  $fn$
"""


def upgrade() -> None:
    op.execute(_PRUNE_STUCK_WITH_PENDING)
    op.execute("REVOKE EXECUTE ON FUNCTION prune_stuck_inspection_jobs(timestamptz) FROM PUBLIC")


def downgrade() -> None:
    op.execute(_PRUNE_STUCK_PROCESSING_ONLY)
    op.execute("REVOKE EXECUTE ON FUNCTION prune_stuck_inspection_jobs(timestamptz) FROM PUBLIC")
