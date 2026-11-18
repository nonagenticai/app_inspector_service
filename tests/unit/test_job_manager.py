"""Unit tests for JobManager — mock-only paths.

Real-db state-transition coverage lives in ``test_job_manager_real_db.py``.
This file targets paths that purely shape ORM objects without atomic SQL
(``create_job``/``get_job``) plus the postgres-specific upsert in
``record_usage`` which is mocked because the statement uses
``sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from src.api.constants import DEFAULT_MAX_SCREENS
from src.services.job_manager import JobManager

# ============================================================================
# create_job
# ============================================================================


class TestCreateJob:
    async def test_create_job_returns_job_with_correct_fields(self):
        """create_job should return an InspectionJob with all fields set correctly."""
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()

        with patch("src.services.job_manager.uuid.uuid7", return_value="fixed-uuid"):
            job = await JobManager.create_job(
                db,
                tenant_id="01950000-0000-7000-8000-000000000001",
                app_path="/tmp/app.apk",
                platform="android",
            )

        assert job.job_id == "fixed-uuid"
        assert job.tenant_id == "01950000-0000-7000-8000-000000000001"
        assert job.status == "pending"
        assert job.platform == "android"
        assert job.app_path == "/tmp/app.apk"
        assert job.automation_backend == "appium"
        assert job.device_farm_provider == "local"
        assert job.max_screens == DEFAULT_MAX_SCREENS
        assert job.enable_recording is False
        assert job.require_selection is False
        db.add.assert_called_once()
        db.flush.assert_awaited_once()

    async def test_create_job_with_all_options(self):
        """create_job should honor all optional parameters."""
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()

        job = await JobManager.create_job(
            db,
            tenant_id="01950000-0000-7000-8000-000000000002",
            app_path="/tmp/app.ipa",
            platform="ios",
            automation_backend="maestro",
            device_farm_provider="browserstack",
            max_screens=50,
            enable_recording=True,
            require_selection=True,
        )

        assert job.platform == "ios"
        assert job.automation_backend == "maestro"
        assert job.device_farm_provider == "browserstack"
        assert job.max_screens == 50
        assert job.enable_recording is True
        assert job.require_selection is True
        assert job.app_path == "/tmp/app.ipa"

    async def test_create_job_generates_unique_ids(self):
        """Two create_job calls should produce different job IDs."""
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()

        job1 = await JobManager.create_job(
            db, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app.apk"
        )
        job2 = await JobManager.create_job(
            db, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app.apk"
        )

        assert job1.job_id != job2.job_id


# ============================================================================
# get_job
# ============================================================================


class TestGetJob:
    async def test_get_existing_job(self):
        """get_job returns whatever scalar_one_or_none yields."""
        mock_job = MagicMock()
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = mock_job
        db.execute = AsyncMock(return_value=result)

        returned = await JobManager.get_job(db, "test-job-1")
        assert returned is mock_job

    async def test_get_missing_job_returns_none(self):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        returned = await JobManager.get_job(db, "nonexistent")
        assert returned is None


# ============================================================================
# record_usage — mocked because the statement uses
# sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update,
# which is exercised more directly in the real-db tests.
# ============================================================================


class TestRecordUsage:
    async def test_record_usage_executes_upsert_statement(self):
        """record_usage should build and execute a PostgreSQL upsert statement."""
        db = AsyncMock()
        db.execute = AsyncMock()

        await JobManager.record_usage(
            db,
            tenant_id="01950000-0000-7000-8000-000000000001",
            inspections_completed=1,
            screens_discovered=5,
        )

        db.execute.assert_awaited_once()
        stmt = db.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "inspection_usage" in compiled
        # ``tenant_id`` is a Postgres ``uuid`` column now; SQLAlchemy's Uuid literal
        # processor renders it DASHLESS ("0195...0001" -> "01950000...0001"), which is
        # still valid uuid input. Assert the hex form, not the hyphenated one.
        assert "01950000-0000-7000-8000-000000000001".replace("-", "") in compiled

    async def test_record_usage_error_flag(self):
        """record_usage with is_error=True should reference inspections_failed."""
        db = AsyncMock()
        db.execute = AsyncMock()

        await JobManager.record_usage(
            db, tenant_id="01950000-0000-7000-8000-000000000001", is_error=True
        )

        db.execute.assert_awaited_once()
        stmt = db.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "inspection_usage" in compiled
        assert "inspections_failed" in compiled

    async def test_record_usage_non_error_does_not_increment_failures(self):
        """record_usage with is_error=False still references inspections_failed (set to 0)."""
        db = AsyncMock()
        db.execute = AsyncMock()

        await JobManager.record_usage(
            db,
            tenant_id="01950000-0000-7000-8000-000000000001",
            is_error=False,
            inspections_completed=1,
        )

        db.execute.assert_awaited_once()
        stmt = db.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "inspections_failed" in compiled
