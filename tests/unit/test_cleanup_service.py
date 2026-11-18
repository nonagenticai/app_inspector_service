"""Integration tests for CleanupService.

Under Phase D RLS the cross-tenant cleanup runs through SECURITY DEFINER resolvers
(``prune_stuck_inspection_jobs`` / ``prune_old_inspection_jobs``). These tests
exercise the real resolver SQL against a live Postgres (the ``db_session`` fixture's
``create_all`` schema includes the resolvers via ``register_resolvers``), seeding
real rows and asserting both the returned count and the resulting DB state — which
is a stronger guarantee than the previous ORM-call mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import RETENTION_DAYS, STUCK_JOB_TIMEOUT_MINUTES
from src.database.models import InspectionJob
from src.services.cleanup_service import CleanupService


async def _seed(
    db: AsyncSession,
    *,
    job_id: str,
    tenant_id: str = "01950000-0000-7000-8000-0000000ac0e5",
    status: str = "processing",
    updated_minutes_ago: float | None = None,
    created_days_ago: float | None = None,
) -> None:
    now = datetime.now(UTC)
    db.add(
        InspectionJob(
            job_id=job_id,
            tenant_id=tenant_id,
            status=status,
            updated_at=(now - timedelta(minutes=updated_minutes_ago))
            if updated_minutes_ago is not None
            else now,
            created_at=(now - timedelta(days=created_days_ago))
            if created_days_ago is not None
            else now,
        )
    )
    await db.commit()


async def _status_of(db: AsyncSession, job_id: str) -> str | None:
    row = (
        await db.execute(select(InspectionJob).where(InspectionJob.job_id == job_id))
    ).scalar_one_or_none()
    return row.status if row else None


class TestCleanupStuckJobs:
    async def test_no_stuck_jobs_returns_zero(self, db_session: AsyncSession) -> None:
        count = await CleanupService().cleanup_stuck_jobs(db_session)
        assert count == 0

    async def test_marks_stuck_jobs_failed_cross_tenant(self, db_session: AsyncSession) -> None:
        # Two stuck 'processing' jobs in DIFFERENT tenants — the resolver sweeps both.
        await _seed(
            db_session,
            job_id="stuck-a",
            tenant_id="01950000-0000-7000-8000-00000000000a",
            updated_minutes_ago=120,
        )
        await _seed(
            db_session,
            job_id="stuck-b",
            tenant_id="01950000-0000-7000-8000-00000000000b",
            updated_minutes_ago=120,
        )
        # A fresh processing job that must NOT be touched.
        await _seed(
            db_session,
            job_id="fresh",
            tenant_id="01950000-0000-7000-8000-00000000000a",
            updated_minutes_ago=1,
        )

        count = await CleanupService().cleanup_stuck_jobs(
            db_session, stuck_timeout_minutes=STUCK_JOB_TIMEOUT_MINUTES
        )

        assert count == 2
        assert await _status_of(db_session, "stuck-a") == "failed"
        assert await _status_of(db_session, "stuck-b") == "failed"
        assert await _status_of(db_session, "fresh") == "processing"

    async def test_custom_timeout_window(self, db_session: AsyncSession) -> None:
        # Stuck 90 min ago: swept by a 60-min window, spared by a 120-min window.
        await _seed(db_session, job_id="mid", updated_minutes_ago=90)
        assert await CleanupService().cleanup_stuck_jobs(db_session, stuck_timeout_minutes=120) == 0
        assert await _status_of(db_session, "mid") == "processing"
        assert await CleanupService().cleanup_stuck_jobs(db_session, stuck_timeout_minutes=60) == 1
        assert await _status_of(db_session, "mid") == "failed"


class TestCleanupOldJobs:
    async def test_no_old_jobs_returns_zero(self, db_session: AsyncSession) -> None:
        count = await CleanupService().cleanup_old_jobs(db_session)
        assert count == 0

    async def test_deletes_old_terminal_jobs_cross_tenant(self, db_session: AsyncSession) -> None:
        await _seed(
            db_session,
            job_id="old-done",
            tenant_id="01950000-0000-7000-8000-00000000000a",
            status="completed",
            created_days_ago=60,
        )
        await _seed(
            db_session,
            job_id="old-fail",
            tenant_id="01950000-0000-7000-8000-00000000000b",
            status="failed",
            created_days_ago=45,
        )
        # Old but still processing → NOT terminal → spared.
        await _seed(db_session, job_id="old-running", status="processing", created_days_ago=60)
        # Recent terminal → spared.
        await _seed(db_session, job_id="recent-done", status="completed", created_days_ago=0)

        count = await CleanupService().cleanup_old_jobs(db_session, retention_days=RETENTION_DAYS)

        assert count == 2
        assert await _status_of(db_session, "old-done") is None
        assert await _status_of(db_session, "old-fail") is None
        assert await _status_of(db_session, "old-running") == "processing"
        assert await _status_of(db_session, "recent-done") == "completed"

    async def test_custom_retention_days(self, db_session: AsyncSession) -> None:
        await _seed(db_session, job_id="ten-day", status="completed", created_days_ago=10)
        assert await CleanupService().cleanup_old_jobs(db_session, retention_days=30) == 0
        assert await _status_of(db_session, "ten-day") == "completed"
        assert await CleanupService().cleanup_old_jobs(db_session, retention_days=7) == 1
        assert await _status_of(db_session, "ten-day") is None


class TestRunSweepSingleFlight:
    """The multi-replica cleanup loop must be single-flight: only the replica
    that wins ``pg_try_advisory_xact_lock`` runs the prune passes; the others
    skip. These mock the advisory-lock result so no real Postgres is needed."""

    async def test_skips_prunes_when_lock_not_acquired(self) -> None:
        db = AsyncMock(spec=AsyncSession)
        lock_result = MagicMock()
        lock_result.scalar.return_value = False  # another replica holds the lock
        db.execute = AsyncMock(return_value=lock_result)

        out = await CleanupService().run_sweep(db)

        assert out is None
        # Only the advisory-lock query ran — no prune SQL, no commit.
        assert db.execute.await_count == 1
        db.commit.assert_not_awaited()

    async def test_runs_prunes_when_lock_acquired(self) -> None:
        db = AsyncMock(spec=AsyncSession)
        lock_result = MagicMock()
        lock_result.scalar.return_value = True
        stuck_result = MagicMock()
        stuck_result.scalar_one.return_value = 2
        old_result = MagicMock()
        old_result.scalar_one.return_value = 3
        db.execute = AsyncMock(side_effect=[lock_result, stuck_result, old_result])

        out = await CleanupService().run_sweep(db)

        assert out == (2, 3)
        # lock + 2 prune passes, then exactly one commit releases the xact lock.
        assert db.execute.await_count == 3
        db.commit.assert_awaited_once()
