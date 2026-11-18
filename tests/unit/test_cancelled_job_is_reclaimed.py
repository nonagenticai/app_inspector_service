"""A job cancelled mid-flight must not sit non-terminal forever.

``start_inspection`` COMMITs the row as ``pending`` and only THEN spawns
``_process_inspection_bg``, whose first DB write is ``mark_processing``. Two ways
that task dies without ever reaching a terminal state:

* ``asyncio.CancelledError`` — a ``BaseException``, so the task body's
  ``except Exception -> mark_failed`` never sees it;
* the pod is SIGTERMed during a rolling deploy right after the 202, closing the
  loop and cancelling every task in ``_background_tasks``.

Before this change the row was then unreachable by BOTH sweeps:
``prune_stuck_inspection_jobs`` matched only ``status = 'processing'`` and
``prune_old_inspection_jobs`` deletes only ``completed``/``failed``. A client
polling ``GET /api/inspect/{job_id}`` saw ``pending`` for the life of the DB.

Two independent guarantees are asserted here, deliberately:
1. the fast path — the ``except asyncio.CancelledError`` clause marks the row
   failed immediately;
2. the durable backstop — the DB sweep reclaims a stranded ``pending`` row even
   when the fast path could not run (interpreter teardown, SIGKILL, a replica
   that vanished). (2) is what makes this correct rather than best-effort.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import STUCK_JOB_TIMEOUT_MINUTES
from src.database.models import InspectionJob
from src.services.cleanup_service import CleanupService

TENANT = "01950000-0000-7000-8000-0000000ac0e5"


async def _seed(db: AsyncSession, *, job_id: str, status: str, updated_minutes_ago: float) -> None:
    now = datetime.now(UTC)
    db.add(
        InspectionJob(
            job_id=job_id,
            tenant_id=TENANT,
            status=status,
            created_at=now - timedelta(minutes=updated_minutes_ago),
            updated_at=now - timedelta(minutes=updated_minutes_ago),
        )
    )
    await db.commit()


async def _status_of(db: AsyncSession, job_id: str) -> str | None:
    db.expire_all()
    row = (
        await db.execute(select(InspectionJob).where(InspectionJob.job_id == job_id))
    ).scalar_one_or_none()
    return row.status if row else None


class TestSweepReclaimsStrandedPending:
    """The durable backstop: the resolver must reach ``pending``, not only
    ``processing``. Mutating the resolver's ``status IN ('pending','processing')``
    back to ``status = 'processing'`` turns this red."""

    async def test_stale_pending_job_is_marked_failed(self, db_session: AsyncSession) -> None:
        await _seed(
            db_session, job_id="stranded-pending", status="pending", updated_minutes_ago=120
        )

        count = await CleanupService().cleanup_stuck_jobs(
            db_session, stuck_timeout_minutes=STUCK_JOB_TIMEOUT_MINUTES
        )

        assert count == 1
        assert await _status_of(db_session, "stranded-pending") == "failed"

    async def test_a_freshly_accepted_pending_job_is_spared(self, db_session: AsyncSession) -> None:
        # A healthy job is 'pending' for milliseconds; the 60-minute cutoff must
        # not race the request that just created it.
        await _seed(db_session, job_id="just-accepted", status="pending", updated_minutes_ago=1)

        assert await CleanupService().cleanup_stuck_jobs(db_session) == 0
        assert await _status_of(db_session, "just-accepted") == "pending"


class TestCancelledBackgroundTaskMarksFailed:
    """The fast path: ``except asyncio.CancelledError`` in ``_process_inspection_bg``.
    Deleting that clause turns this red — the row stays 'pending'."""

    async def test_cancelling_the_task_marks_the_job_failed(
        self, db_session: AsyncSession, db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.api.routes import inspections
        from src.services.inspector_service import InspectorService

        await _seed(db_session, job_id="cancel-me", status="pending", updated_minutes_ago=0)

        @contextlib.asynccontextmanager
        async def _fake_session():
            async with db_session_factory() as session:
                yield session

        monkeypatch.setattr(inspections, "get_db_session", _fake_session)

        started = asyncio.Event()

        class _HangingService:
            async def process_job(self, db: Any, job_id: str, tenant_id: str, **_kw: Any) -> None:
                started.set()
                await asyncio.sleep(3600)  # still running when the pod goes away

        task = asyncio.create_task(
            inspections._process_inspection_bg(
                cast("InspectorService", _HangingService()),
                "cancel-me",
                TENANT,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _status_of(db_session, "cancel-me") == "failed"
