"""Cleanup service for stale inspection jobs and expired artifacts.

These sweeps are CROSS-TENANT (the background loop runs at app boot with no tenant
context). Under Phase D RLS enforce the runtime is a non-bypass role with no
``app.current_tenant`` bound, so an ORM read/write would fail-closed to 0 rows.
The maintenance therefore runs through SECURITY DEFINER resolvers
(``prune_stuck_inspection_jobs`` / ``prune_old_inspection_jobs``, migration 0006)
owned by the table owner, which bypass ENABLE and operate across all tenants. The
resolvers run as the same owner under mode=off too, so this path is correct before
and after cutover.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import RETENTION_DAYS, STUCK_JOB_TIMEOUT_MINUTES

logger = logging.getLogger(__name__)

# Stable, app_inspector-private key for the cross-replica cleanup mutex. Advisory
# locks are per-database and this service owns its database, so the value only
# needs to be recognisable in ``pg_locks`` — it is the service port (6311).
_CLEANUP_LOCK_KEY = -6311


class CleanupService:
    """Periodic cleanup of stuck-processing and retention-expired jobs."""

    async def cleanup_stuck_jobs(
        self,
        db: AsyncSession,
        *,
        stuck_timeout_minutes: int = STUCK_JOB_TIMEOUT_MINUTES,
    ) -> int:
        """Mark stuck processing jobs as failed (cross-tenant). Returns the count."""
        cutoff = datetime.now(UTC) - timedelta(minutes=stuck_timeout_minutes)
        count = (await db.execute(select(func.prune_stuck_inspection_jobs(cutoff)))).scalar_one()
        await db.commit()
        if count:
            logger.info("Marked %d stuck inspection jobs failed", count)
        return int(count or 0)

    async def cleanup_old_jobs(
        self,
        db: AsyncSession,
        *,
        retention_days: int = RETENTION_DAYS,
    ) -> int:
        """Delete terminal inspection jobs older than retention (cross-tenant)."""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        count = (await db.execute(select(func.prune_old_inspection_jobs(cutoff)))).scalar_one()
        await db.commit()
        if count:
            logger.info("Deleted %d old inspection jobs", count)
        return int(count or 0)

    async def run_sweep(
        self,
        db: AsyncSession,
        *,
        stuck_timeout_minutes: int = STUCK_JOB_TIMEOUT_MINUTES,
        retention_days: int = RETENTION_DAYS,
    ) -> tuple[int, int] | None:
        """Run the full cleanup sweep once, guarded by a cross-replica advisory lock.

        The service runs multiple replicas; every replica ticks its own cleanup
        loop, so without coordination all N would sweep on each tick — redundant,
        and the stuck-job timeout pass can race a job that is legitimately still
        running on another replica. ``pg_try_advisory_xact_lock`` is a
        non-blocking, transaction-scoped mutex: exactly one replica acquires it
        per tick and does the work; the others get ``False`` and skip. The lock
        is held across BOTH prune passes and auto-released by the single commit
        (or a rollback), so it is impossible to leak.

        Returns ``(stuck_marked, old_deleted)`` for the replica that swept, or
        ``None`` for a replica that did not acquire the lock.
        """
        locked = (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": _CLEANUP_LOCK_KEY},
            )
        ).scalar()
        if not locked:
            logger.debug("Cleanup: another replica holds the sweep lock; skipping this tick")
            return None

        now = datetime.now(UTC)
        stuck = (
            await db.execute(
                select(
                    func.prune_stuck_inspection_jobs(now - timedelta(minutes=stuck_timeout_minutes))
                )
            )
        ).scalar_one()
        old = (
            await db.execute(
                select(func.prune_old_inspection_jobs(now - timedelta(days=retention_days)))
            )
        ).scalar_one()
        # Single commit releases the transaction-scoped advisory lock.
        await db.commit()

        stuck_n, old_n = int(stuck or 0), int(old or 0)
        if stuck_n:
            logger.info("Marked %d stuck inspection jobs failed", stuck_n)
        if old_n:
            logger.info("Deleted %d old inspection jobs", old_n)
        return stuck_n, old_n
