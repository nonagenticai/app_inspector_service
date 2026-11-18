"""Phase D RLS — enable row-level security on the tenant-scoped tables.

Applies the SAME policies the ``create_all`` ``register_rls_ddl`` event applies in
tests, rendered from the single source ``w2v_common.database.rls`` (inspection_jobs
+ inspection_usage; both varchar tenant_id → text predicate). ENABLE-not-FORCE →
inert until the runtime connects as the restricted non-owner role
``w2v_app_inspector_app`` with ``TENANT_RLS_MODE=enforce``.

Also creates two SECURITY DEFINER resolvers owned by the migration user (table
owner → bypasses ENABLE) so the cross-tenant cleanup loop (``cleanup_service.py``,
started at app boot with no tenant context) still maintains jobs across ALL tenants
under enforce — a non-bypass role with no GUC would otherwise fail-closed to 0 rows.
EXECUTE on these is REVOKEd from PUBLIC here and GRANTed to the app role out-of-band
in Stage A (the app role does not exist in CI).

Revision ID: 0006_rls
Revises: 0005
"""

from alembic import op


revision: str = "0006_rls"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None


# ⛔ FROZEN 2026-09-02 — these statements used to be rendered at run time from the
# LIVE ``Base.metadata`` via ``w2v_common.database.rls.{up,down}grade_statements``.
# That makes a historical migration describe TODAY's ORM instead of the schema that
# existed when it ran: ``_predicate()`` renders ``tenant_id = <guc>`` for a
# ``String`` column and ``tenant_id = <guc>::uuid`` for anything else, so the moment
# the ORM column became ``uuid`` this migration started emitting a ``::uuid``
# predicate against a column that is still ``VARCHAR(128)`` at this point in the
# chain — ``operator does not exist: character varying = uuid`` — and
# ``alembic upgrade head`` from ZERO broke. The statements below are byte-for-byte
# what the helpers rendered at the time (varchar tenant_id, all NOT NULL → strict
# equality), pinned as literals. A migration is a historical record; it must not
# move when the models move. The type change itself lives in ``0007_tenant_uuid``.
_GUC = "NULLIF(current_setting('app.current_tenant', true), '')"
_PREDICATE = f"tenant_id = {_GUC}"  # varchar column → text-to-text comparison
_TENANT_TABLES: tuple[str, ...] = (
    "inspection_jobs",
    "inspection_usage",
)
_ALL_POLICY_NAMES = (
    "tenant_isolation",
    "tenant_isolation_read",
    "tenant_isolation_ins",
    "tenant_isolation_upd",
    "tenant_isolation_del",
)


# ⛔ FROZEN 2026-09-04 — these two resolver bodies used to be imported from
# ``src.database.rls_resolvers``, which meant this historical migration emitted
# TODAY's resolver instead of the one that existed when it ran. ``0008`` widens
# the stuck sweep to ``pending``; that belongs to ``0008``, not here. Same rule
# as the RLS statements above: a migration is a historical record.
_PRUNE_STUCK_SQL_0006 = """
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

_PRUNE_OLD_SQL_0006 = """
CREATE OR REPLACE FUNCTION prune_old_inspection_jobs(p_cutoff timestamptz)
  RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    WITH d AS (
      DELETE FROM inspection_jobs
      WHERE created_at < p_cutoff AND status IN ('completed', 'failed')
      RETURNING 1
    ) SELECT count(*)::int FROM d;
  $fn$
"""


def _rls_upgrade_statements() -> list[str]:
    out: list[str] = []
    for table in _TENANT_TABLES:
        out += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})",
        ]
    return out


def _rls_downgrade_statements() -> list[str]:
    out: list[str] = []
    for table in _TENANT_TABLES:
        out += [f"DROP POLICY IF EXISTS {policy} ON {table}" for policy in _ALL_POLICY_NAMES]
        out.append(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    return out


def upgrade() -> None:
    for stmt in _rls_upgrade_statements():
        op.execute(stmt)
    op.execute(_PRUNE_STUCK_SQL_0006)
    op.execute("REVOKE EXECUTE ON FUNCTION prune_stuck_inspection_jobs(timestamptz) FROM PUBLIC")
    op.execute(_PRUNE_OLD_SQL_0006)
    op.execute("REVOKE EXECUTE ON FUNCTION prune_old_inspection_jobs(timestamptz) FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS prune_old_inspection_jobs(timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS prune_stuck_inspection_jobs(timestamptz)")
    for stmt in _rls_downgrade_statements():
        op.execute(stmt)
