"""Job lifecycle management for app inspection jobs."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import DEFAULT_MAX_SCREENS, MAX_ERROR_LENGTH
from src.database.models import InspectionJob, InspectionUsage
from src.utils.redaction import redact_error_message

logger = logging.getLogger(__name__)


class JobStateConflictError(RuntimeError):
    """Raised when an atomic job-state transition loses a race.

    A caller should treat this as "another worker already transitioned
    this job" — log and bail cleanly, do not retry.
    """


class JobManager:
    """Static methods for inspection job CRUD and usage tracking.

    All methods operate on ``InspectionJob`` rows and are designed
    to be called within an existing async database session.
    """

    @staticmethod
    async def create_job(
        db: AsyncSession,
        *,
        tenant_id: str,
        app_path: str,
        platform: str | None = None,
        automation_backend: str = "appium",
        device_farm_provider: str = "local",
        max_screens: int = DEFAULT_MAX_SCREENS,
        enable_recording: bool = False,
        require_selection: bool = False,
    ) -> InspectionJob:
        """Create a new inspection job row.

        Args:
            db: Active async session.
            tenant_id: Owning tenant identifier.
            app_path: Path or URL to the app binary.
            platform: Target platform (ios/android).
            automation_backend: Backend to use (appium/maestro).
            device_farm_provider: Farm provider name.
            max_screens: Max screens to discover.
            enable_recording: Whether to record the session.
            require_selection: Whether screen selection is required.

        Returns:
            The newly created InspectionJob instance.
        """
        job = InspectionJob(
            job_id=str(uuid.uuid7()),
            tenant_id=tenant_id,
            app_path=app_path,
            platform=platform,
            automation_backend=automation_backend,
            device_farm_provider=device_farm_provider,
            max_screens=max_screens,
            status="pending",
            enable_recording=enable_recording,
            require_selection=require_selection,
        )
        db.add(job)
        await db.flush()
        logger.debug("Created job %s for tenant=%s", job.job_id, tenant_id)
        return job

    @staticmethod
    async def get_job(
        db: AsyncSession,
        job_id: str,
        *,
        tenant_id: str | None = None,
    ) -> InspectionJob | None:
        """Fetch an inspection job by its job_id, optionally scoped to a tenant.

        Every request-path lookup MUST pass ``tenant_id``: a job_id is the only
        thing an API caller supplies, so an unscoped lookup lets any tenant read
        (or write) another tenant's job by id. A tenant mismatch returns ``None``
        — indistinguishable from a genuinely missing row, so the caller's
        not-found branch answers 404 rather than a 403 that would confirm the id
        exists.

        Args:
            db: Active async session.
            job_id: Unique job identifier.
            tenant_id: When set, the row must also belong to this tenant.

        Returns:
            The InspectionJob or None if not found (or owned by another tenant).
        """
        logger.debug("Fetching job %s (tenant=%s)", job_id, tenant_id)
        stmt = select(InspectionJob).where(InspectionJob.job_id == job_id)
        if tenant_id is not None:
            stmt = stmt.where(InspectionJob.tenant_id == tenant_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def mark_processing(db: AsyncSession, job_id: str) -> None:
        """Atomically transition a pending job to the processing state.

        Raises :class:`JobStateConflictError` if the job is not currently pending.
        """
        result = cast(
            CursorResult[Any],
            await db.execute(
                update(InspectionJob)
                .where(InspectionJob.job_id == job_id, InspectionJob.status == "pending")
                .values(status="processing", updated_at=datetime.now(UTC))
            ),
        )
        await db.commit()
        if result.rowcount == 0:
            raise JobStateConflictError(
                f"Inspection job {job_id!r} is not pending — cannot transition to processing"
            )
        logger.debug("Job %s marked as processing", job_id)

    @staticmethod
    async def mark_completed(
        db: AsyncSession,
        job_id: str,
        *,
        screens_discovered: int = 0,
        screens_data: list | None = None,
        navigation_graph: list[dict[str, str | None]] | None = None,
        inspection_time_seconds: float | None = None,
        video_path: str | None = None,
        estimated_purpose: str | None = None,
    ) -> None:
        """Atomically transition a processing job to the completed state.

        Raises :class:`JobStateConflictError` if the job is not currently processing.
        """
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "status": "completed",
            "screens_discovered": screens_discovered,
            "screens_data": screens_data or [],
            "navigation_graph": navigation_graph or [],
            "inspection_time_seconds": inspection_time_seconds,
            "updated_at": now,
            "completed_at": now,
        }
        if video_path is not None:
            values["video_path"] = video_path
        if estimated_purpose is not None:
            values["estimated_purpose"] = estimated_purpose

        result = cast(
            CursorResult[Any],
            await db.execute(
                update(InspectionJob)
                .where(InspectionJob.job_id == job_id, InspectionJob.status == "processing")
                .values(**values)
            ),
        )
        await db.commit()
        if result.rowcount == 0:
            raise JobStateConflictError(
                f"Inspection job {job_id!r} is not processing — cannot transition to completed"
            )
        logger.debug(
            "Job %s marked completed: %d screens in %.1fs",
            job_id,
            screens_discovered,
            inspection_time_seconds or 0,
        )

    @staticmethod
    async def mark_failed(db: AsyncSession, job_id: str, error: str) -> None:
        """Atomically transition a non-terminal job to the failed state.

        Raises :class:`JobStateConflictError` if the job is already terminal.
        """
        now = datetime.now(UTC)
        result = cast(
            CursorResult[Any],
            await db.execute(
                update(InspectionJob)
                .where(
                    InspectionJob.job_id == job_id,
                    InspectionJob.status.in_(["pending", "processing"]),
                )
                .values(
                    status="failed",
                    error_message=redact_error_message(error)[:MAX_ERROR_LENGTH],
                    updated_at=now,
                    completed_at=now,
                )
            ),
        )
        await db.commit()
        if result.rowcount == 0:
            raise JobStateConflictError(
                f"Inspection job {job_id!r} is already terminal — cannot transition to failed"
            )
        logger.debug("Job %s marked as failed", job_id)

    @staticmethod
    async def update_progress(
        db: AsyncSession,
        job_id: str,
        *,
        screens_discovered: int = 0,
    ) -> None:
        """Update progress for an in-flight inspection.

        Only applies when the job is still processing — terminal jobs
        are untouched, silently, since progress bumps are advisory.
        """
        await db.execute(
            update(InspectionJob)
            .where(InspectionJob.job_id == job_id, InspectionJob.status == "processing")
            .values(screens_discovered=screens_discovered, updated_at=datetime.now(UTC))
        )
        await db.commit()

    @staticmethod
    async def mark_failed_with_partial(
        db: AsyncSession,
        job_id: str,
        error: str,
        *,
        partial_screens_data: list | None = None,
    ) -> None:
        """Atomically transition a non-terminal job to failed, preserving partial data.

        Raises :class:`JobStateConflictError` if the job is already terminal.
        """
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "status": "failed",
            "error_message": redact_error_message(error)[:MAX_ERROR_LENGTH],
            "updated_at": now,
            "completed_at": now,
        }
        if partial_screens_data:
            values["screens_data"] = partial_screens_data
            values["screens_discovered"] = len(partial_screens_data)

        result = cast(
            CursorResult[Any],
            await db.execute(
                update(InspectionJob)
                .where(
                    InspectionJob.job_id == job_id,
                    InspectionJob.status.in_(["pending", "processing"]),
                )
                .values(**values)
            ),
        )
        await db.commit()
        if result.rowcount == 0:
            raise JobStateConflictError(
                f"Inspection job {job_id!r} is already terminal — cannot mark_failed_with_partial"
            )

    @staticmethod
    async def mark_awaiting_selection(
        db: AsyncSession,
        job_id: str,
        *,
        screens_discovered: int | None = None,
        screens_data: list | None = None,
        navigation_graph: list[dict[str, str | None]] | None = None,
        inspection_time_seconds: float | None = None,
        video_path: str | None = None,
        estimated_purpose: str | None = None,
    ) -> None:
        """Atomically transition a processing job to the awaiting_selection state.

        Optional data fields are written in the same atomic UPDATE so callers
        don't have to patch the row via ORM attribute assignment first.

        Raises :class:`JobStateConflictError` if the job is not currently processing.
        """
        values: dict[str, Any] = {
            "status": "awaiting_selection",
            "updated_at": datetime.now(UTC),
        }
        if screens_discovered is not None:
            values["screens_discovered"] = screens_discovered
        if screens_data is not None:
            values["screens_data"] = screens_data
        if navigation_graph is not None:
            values["navigation_graph"] = navigation_graph
        if inspection_time_seconds is not None:
            values["inspection_time_seconds"] = inspection_time_seconds
        if video_path is not None:
            values["video_path"] = video_path
        if estimated_purpose is not None:
            values["estimated_purpose"] = estimated_purpose

        result = cast(
            CursorResult[Any],
            await db.execute(
                update(InspectionJob)
                .where(InspectionJob.job_id == job_id, InspectionJob.status == "processing")
                .values(**values)
            ),
        )
        await db.commit()
        if result.rowcount == 0:
            raise JobStateConflictError(
                f"Inspection job {job_id!r} is not processing — "
                f"cannot transition to awaiting_selection"
            )
        logger.debug("Job %s marked as awaiting_selection", job_id)

    @staticmethod
    async def update_screen_selection(
        db: AsyncSession,
        job_id: str,
        selected_screen_ids: list[str],
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Save the user's screen selection and complete the job.

        Only transitions when the job is currently ``awaiting_selection``.

        The ``tenant_id`` predicate is the SECOND layer: the route already 404s
        on a tenant mismatch via the scoped :meth:`get_job`, but that read must
        not be the only thing standing between a caller and another tenant's
        row. When set, a foreign job_id matches zero rows and raises
        ``JobStateConflictError`` instead of writing.

        Args:
            db: Active async session.
            job_id: Unique job identifier.
            selected_screen_ids: Screen ids the user kept.
            tenant_id: When set, the row must also belong to this tenant.

        Raises:
            JobStateConflictError: If the job is not awaiting selection (or is
                owned by another tenant).
        """
        now = datetime.now(UTC)
        stmt = update(InspectionJob).where(
            InspectionJob.job_id == job_id,
            InspectionJob.status == "awaiting_selection",
        )
        if tenant_id is not None:
            stmt = stmt.where(InspectionJob.tenant_id == tenant_id)
        result = cast(
            CursorResult[Any],
            await db.execute(
                stmt.values(
                    selected_screen_ids=selected_screen_ids,
                    status="completed",
                    updated_at=now,
                    completed_at=now,
                )
            ),
        )
        await db.commit()
        if result.rowcount == 0:
            raise JobStateConflictError(
                f"Inspection job {job_id!r} is not awaiting_selection — "
                f"cannot apply screen selection"
            )
        logger.debug(
            "Job %s screen selection updated: %d screens", job_id, len(selected_screen_ids)
        )

    @staticmethod
    async def record_usage(
        db: AsyncSession,
        *,
        tenant_id: str,
        inspections_completed: int = 0,
        is_error: bool = False,
        screens_discovered: int = 0,
    ) -> None:
        """Upsert daily usage counters for a tenant.

        Args:
            db: Active async session.
            tenant_id: Tenant identifier.
            inspections_completed: Number of completed inspections to add.
            is_error: Whether this invocation is for an error.
            screens_discovered: Number of screens discovered to add.
        """
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        stmt = (
            pg_insert(InspectionUsage)
            .values(
                tenant_id=tenant_id,
                date=today,
                inspections_started=1,
                inspections_completed=inspections_completed,
                inspections_failed=1 if is_error else 0,
                total_screens_discovered=screens_discovered,
            )
            .on_conflict_do_update(
                constraint="uq_inspection_usage_tenant_date",
                set_={
                    "inspections_started": text("inspection_usage.inspections_started + 1"),
                    "inspections_completed": text(
                        f"inspection_usage.inspections_completed + {inspections_completed}"
                    ),
                    "inspections_failed": text(
                        f"inspection_usage.inspections_failed + {1 if is_error else 0}"
                    ),
                    "total_screens_discovered": text(
                        f"inspection_usage.total_screens_discovered + {screens_discovered}"
                    ),
                },
            )
        )
        await db.execute(stmt)
