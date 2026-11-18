"""SECURITY DEFINER cross-tenant maintenance resolvers (single source).

The cleanup loop is cross-tenant and runs with no tenant context; under RLS enforce
the runtime (a non-bypass role) would fail-closed to 0 rows. These resolvers are
owned by the table owner, so they bypass ENABLE and operate across all tenants.

The DDL is single-sourced here so BOTH the alembic migration (prod) and the
``create_all`` ``after_create`` event (tests) create identical functions. The
migration additionally ``REVOKE``s EXECUTE from PUBLIC and Stage A grants it to the
app role; tests run as a superuser so EXECUTE works without the grant.
"""

from __future__ import annotations

from sqlalchemy import Connection, MetaData, event, text

PRUNE_STUCK_SQL = """
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
# ``pending`` is LOAD-BEARING here, not defensive breadth. ``start_inspection``
# COMMITS the row as ``pending`` and only THEN spawns ``_process_inspection_bg``,
# which calls ``JobManager.mark_processing`` as its first DB write. Anything that
# kills the task inside that window leaves the row ``pending`` forever:
#   * a rolling deploy / SIGTERM right after the 202 — the event loop closes and
#     every task in ``_background_tasks`` is cancelled;
#   * ``asyncio.CancelledError`` generally — it is a ``BaseException``, so the
#     task body's ``except Exception -> mark_failed`` never runs.
# ``prune_old_inspection_jobs`` only deletes ``completed``/``failed``, so such a
# row was neither reclaimed NOR retained-out: the poller on
# ``GET /api/inspect/{job_id}`` saw ``pending`` for the life of the database.
# A genuinely pending job is pending for milliseconds, so a cutoff of
# ``STUCK_JOB_TIMEOUT_MINUTES`` (60) cannot race a healthy one.

PRUNE_OLD_SQL = """
CREATE OR REPLACE FUNCTION prune_old_inspection_jobs(p_cutoff timestamptz)
  RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    WITH d AS (
      DELETE FROM inspection_jobs
      WHERE created_at < p_cutoff AND status IN ('completed', 'failed')
      RETURNING 1
    ) SELECT count(*)::int FROM d;
  $fn$
"""


def register_resolvers(metadata: MetaData) -> None:
    """Create the prune resolvers after the tables exist (test create_all path)."""

    def _apply(target: object, connection: Connection, **kw: object) -> None:
        connection.execute(text(PRUNE_STUCK_SQL))
        connection.execute(text(PRUNE_OLD_SQL))

    event.listen(metadata, "after_create", _apply)
