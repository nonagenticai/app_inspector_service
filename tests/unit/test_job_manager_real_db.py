"""Real-database tests for JobManager using postgres."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.api.constants import MAX_ERROR_LENGTH
from src.services.job_manager import JobManager, JobStateConflictError


@pytest.mark.asyncio
class TestJobManager:
    """Tests for JobManager static methods against a real postgres database."""

    # ------------------------------------------------------------------
    # CRUD basics
    # ------------------------------------------------------------------

    async def test_create_and_get_job(self, db_session):
        """create_job persists a row that get_job can retrieve."""
        job = await JobManager.create_job(
            db_session,
            tenant_id="01950000-0000-7000-8000-000000000001",
            app_path="/apps/myapp.apk",
            platform="android",
        )
        assert job.job_id is not None
        assert job.tenant_id == "01950000-0000-7000-8000-000000000001"
        assert job.app_path == "/apps/myapp.apk"
        assert job.platform == "android"
        assert job.status == "pending"
        assert job.automation_backend == "appium"
        assert job.device_farm_provider == "local"
        assert job.max_screens == 20
        assert job.enable_recording is False
        assert job.require_selection is False

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.job_id == job.job_id
        assert fetched.tenant_id == "01950000-0000-7000-8000-000000000001"

    async def test_get_nonexistent_returns_none(self, db_session):
        """get_job returns None for a missing job_id."""
        result = await JobManager.get_job(db_session, "does-not-exist")
        assert result is None

    async def test_create_job_with_all_options(self, db_session):
        """create_job respects all keyword arguments."""
        job = await JobManager.create_job(
            db_session,
            tenant_id="01950000-0000-7000-8000-000000000002",
            app_path="https://example.com/app.ipa",
            platform="ios",
            automation_backend="maestro",
            device_farm_provider="browserstack",
            max_screens=5,
            enable_recording=True,
            require_selection=True,
        )
        assert job.platform == "ios"
        assert job.automation_backend == "maestro"
        assert job.device_farm_provider == "browserstack"
        assert job.max_screens == 5
        assert job.enable_recording is True
        assert job.require_selection is True

    # ------------------------------------------------------------------
    # JSONB round-trips
    # ------------------------------------------------------------------

    async def test_screens_data_jsonb_roundtrip(self, db_session, sample_screens_data):
        """screens_data (list[dict]) survives a write-read cycle."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)
        await JobManager.mark_completed(
            db_session,
            job.job_id,
            screens_discovered=2,
            screens_data=sample_screens_data,
        )
        await db_session.flush()

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.screens_data == sample_screens_data

    async def test_navigation_graph_jsonb_roundtrip(self, db_session, sample_nav_graph):
        """navigation_graph (list[dict]) survives a write-read cycle."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)
        await JobManager.mark_completed(
            db_session,
            job.job_id,
            navigation_graph=sample_nav_graph,
        )
        await db_session.flush()

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.navigation_graph == sample_nav_graph

    async def test_selected_screen_ids_jsonb_roundtrip(self, db_session):
        """selected_screen_ids (list[str]) survives a write-read cycle."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        ids = ["screen_0", "screen_2", "screen_5"]
        await JobManager.mark_processing(db_session, job.job_id)
        await JobManager.mark_awaiting_selection(db_session, job.job_id)
        await JobManager.update_screen_selection(db_session, job.job_id, ids)
        await db_session.flush()

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.selected_screen_ids == ids

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    async def test_pending_to_processing_to_completed(self, db_session):
        """Happy path: pending -> processing -> completed."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        assert job.status == "pending"

        await JobManager.mark_processing(db_session, job.job_id)
        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "processing"

        await JobManager.mark_completed(
            db_session,
            job.job_id,
            screens_discovered=3,
            inspection_time_seconds=12.5,
            video_path="/videos/rec.mp4",
            estimated_purpose="e-commerce app",
        )
        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "completed"
        assert fetched.screens_discovered == 3
        assert fetched.inspection_time_seconds == pytest.approx(12.5)
        assert fetched.video_path == "/videos/rec.mp4"
        assert fetched.estimated_purpose == "e-commerce app"
        assert fetched.completed_at is not None

    async def test_pending_to_processing_to_failed(self, db_session):
        """Failure path: pending -> processing -> failed."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)

        await JobManager.mark_failed(db_session, job.job_id, "Connection refused")
        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "failed"
        assert fetched.error_message == "Connection refused"
        assert fetched.completed_at is not None

    async def test_awaiting_selection_flow(self, db_session):
        """Selection flow: pending -> processing -> awaiting_selection -> completed."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)

        await JobManager.mark_awaiting_selection(db_session, job.job_id)
        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "awaiting_selection"

        screen_ids = ["screen_0", "screen_1"]
        await JobManager.update_screen_selection(db_session, job.job_id, screen_ids)
        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "completed"
        assert fetched.selected_screen_ids == screen_ids
        assert fetched.completed_at is not None

    async def test_update_screen_selection_nonexistent_raises(self, db_session):
        """update_screen_selection raises JobStateConflictError for missing job."""
        with pytest.raises(JobStateConflictError):
            await JobManager.update_screen_selection(db_session, "no-such-id", ["screen_0"])

    # ------------------------------------------------------------------
    # Atomic state transition / race conditions
    # ------------------------------------------------------------------

    async def test_mark_processing_twice_raises_conflict(self, db_session):
        """A second mark_processing on an already-processing job raises conflict."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)
        with pytest.raises(JobStateConflictError):
            await JobManager.mark_processing(db_session, job.job_id)

    async def test_mark_completed_on_pending_raises(self, db_session):
        """mark_completed must not succeed on a job that never entered processing."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        with pytest.raises(JobStateConflictError):
            await JobManager.mark_completed(db_session, job.job_id)

    async def test_mark_failed_on_completed_raises(self, db_session):
        """mark_failed cannot move a terminal (completed) job to failed."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)
        await JobManager.mark_completed(db_session, job.job_id)
        with pytest.raises(JobStateConflictError):
            await JobManager.mark_failed(db_session, job.job_id, "late error")

    async def test_update_progress_is_silent_on_terminal(self, db_session):
        """update_progress on a terminal job is a no-op (advisory counter)."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)
        await JobManager.mark_completed(db_session, job.job_id, screens_discovered=5)
        # Should not raise; should not clobber the existing count.
        await JobManager.update_progress(db_session, job.job_id, screens_discovered=999)

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.screens_discovered == 5

    async def test_mark_processing_race_exactly_one_winner(self, db_engine):
        """Three sessions racing on mark_processing: exactly one wins."""
        session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with session_factory() as seed:
            job = await JobManager.create_job(
                seed, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
            )
            await seed.commit()
            target_id = job.job_id

        async def _worker() -> str:
            async with session_factory() as session:
                try:
                    await JobManager.mark_processing(session, target_id)
                    return "won"
                except JobStateConflictError:
                    return "lost"

        results = await asyncio.gather(_worker(), _worker(), _worker())
        assert sorted(results) == ["lost", "lost", "won"]

        async with session_factory() as check:
            final = await JobManager.get_job(check, target_id)
            assert final is not None
            assert final.status == "processing"

    # ------------------------------------------------------------------
    # Progress updates
    # ------------------------------------------------------------------

    async def test_update_progress(self, db_session):
        """update_progress sets screens_discovered on the job."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)

        await JobManager.update_progress(db_session, job.job_id, screens_discovered=3)
        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.screens_discovered == 3

        await JobManager.update_progress(db_session, job.job_id, screens_discovered=7)
        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.screens_discovered == 7

    # ------------------------------------------------------------------
    # Partial failure
    # ------------------------------------------------------------------

    async def test_mark_failed_with_partial(self, db_session, sample_screens_data):
        """mark_failed_with_partial stores screens_data even on failure."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)

        await JobManager.mark_failed_with_partial(
            db_session,
            job.job_id,
            "Timeout after 5 screens",
            partial_screens_data=sample_screens_data,
        )
        await db_session.flush()

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "failed"
        assert fetched.error_message == "Timeout after 5 screens"
        assert fetched.screens_data == sample_screens_data
        assert fetched.screens_discovered == len(sample_screens_data)
        assert fetched.completed_at is not None

    async def test_mark_failed_with_partial_no_data(self, db_session):
        """mark_failed_with_partial without partial data leaves screens_data untouched."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)

        await JobManager.mark_failed_with_partial(db_session, job.job_id, "Total failure")
        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "failed"
        assert fetched.error_message == "Total failure"
        # screens_data should remain at its default (not overwritten)
        assert fetched.screens_discovered in (0, None)

    # ------------------------------------------------------------------
    # Error truncation
    # ------------------------------------------------------------------

    async def test_mark_failed_truncates_long_error(self, db_session):
        """mark_failed truncates error_message to MAX_ERROR_LENGTH."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        long_error = "x" * (MAX_ERROR_LENGTH + 500)
        await JobManager.mark_failed(db_session, job.job_id, long_error)

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.error_message is not None
        assert len(fetched.error_message) == MAX_ERROR_LENGTH

    async def test_mark_failed_with_partial_truncates_long_error(self, db_session):
        """mark_failed_with_partial also truncates error_message."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        long_error = "y" * (MAX_ERROR_LENGTH + 100)
        await JobManager.mark_failed_with_partial(db_session, job.job_id, long_error)

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.error_message is not None
        assert len(fetched.error_message) == MAX_ERROR_LENGTH

    # ------------------------------------------------------------------
    # Completed defaults
    # ------------------------------------------------------------------

    async def test_mark_completed_defaults(self, db_session):
        """mark_completed with no extra args sets sensible defaults."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)
        await JobManager.mark_completed(db_session, job.job_id)
        await db_session.flush()

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "completed"
        assert fetched.screens_discovered == 0
        assert fetched.screens_data == []
        assert fetched.navigation_graph == []
        assert fetched.inspection_time_seconds is None
        assert fetched.video_path is None
        assert fetched.estimated_purpose is None
