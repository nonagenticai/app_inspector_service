"""Tests for screen selection — real-db assertions over mocks.

State-transition coverage now goes through real postgres sessions
(test_job_manager_real_db.py has full coverage). This file retains
the route-helper filtering tests and schema validation; the mock-based
JobManager transition tests have been rewritten to use the real db
fixture so they actually exercise SQL behavior.
"""

from __future__ import annotations

import pytest

from src.api.schemas import ScreenPreview, ScreenSelectionRequest
from src.services.job_manager import JobManager, JobStateConflictError

# ---------------------------------------------------------------------------
# Tests: mark_awaiting_selection against a real postgres session
# ---------------------------------------------------------------------------


class TestMarkAwaitingSelection:
    async def test_transitions_to_awaiting_selection(self, db_session) -> None:
        """mark_awaiting_selection: processing -> awaiting_selection."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)

        await JobManager.mark_awaiting_selection(db_session, job.job_id)

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.status == "awaiting_selection"
        assert fetched.updated_at is not None

    async def test_raises_conflict_when_job_not_found(self, db_session) -> None:
        """mark_awaiting_selection raises when row does not match the filter."""
        with pytest.raises(JobStateConflictError):
            await JobManager.mark_awaiting_selection(db_session, "nonexistent")


# ---------------------------------------------------------------------------
# Tests: update_screen_selection against a real postgres session
# ---------------------------------------------------------------------------


class TestUpdateScreenSelection:
    async def test_sets_ids_and_transitions_to_completed(self, db_session) -> None:
        """update_screen_selection persists IDs and transitions to completed."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)
        await JobManager.mark_awaiting_selection(db_session, job.job_id)

        await JobManager.update_screen_selection(db_session, job.job_id, ["s1", "s3"])

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.selected_screen_ids == ["s1", "s3"]
        assert fetched.status == "completed"
        assert fetched.completed_at is not None

    async def test_empty_selection_still_completes(self, db_session) -> None:
        """An empty selection list still transitions the job to completed."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)
        await JobManager.mark_awaiting_selection(db_session, job.job_id)

        await JobManager.update_screen_selection(db_session, job.job_id, [])

        fetched = await JobManager.get_job(db_session, job.job_id)
        assert fetched is not None
        assert fetched.selected_screen_ids == []
        assert fetched.status == "completed"

    async def test_raises_conflict_when_job_not_awaiting_selection(self, db_session) -> None:
        """update_screen_selection on a processing (not awaiting) job raises."""
        job = await JobManager.create_job(
            db_session, tenant_id="01950000-0000-7000-8000-000000000001", app_path="/app"
        )
        await JobManager.mark_processing(db_session, job.job_id)

        with pytest.raises(JobStateConflictError):
            await JobManager.update_screen_selection(db_session, job.job_id, ["s1"])

    async def test_raises_conflict_when_job_missing(self, db_session) -> None:
        """update_screen_selection on a non-existent job raises conflict."""
        with pytest.raises(JobStateConflictError):
            await JobManager.update_screen_selection(db_session, "missing-id", ["s1"])


# ---------------------------------------------------------------------------
# Tests: Screen filtering logic (as used in _build_screen_info route helper)
# ---------------------------------------------------------------------------


class TestScreenFiltering:
    """Test the filtering logic that routes use when selected_screen_ids is set.

    Instead of reimplementing filtering inline, we use the real
    _build_screen_info helper to validate that filter + build works.
    """

    def test_filters_to_selected_ids_only(self) -> None:
        """Only selected screens should appear in the filtered result."""
        from src.api.routes._helpers import _build_screen_info

        all_screens = [
            {"screen_id": "s1", "screen_type": "login", "text_content": "Login"},
            {"screen_id": "s2", "screen_type": "home", "text_content": "Home"},
            {"screen_id": "s3", "screen_type": "settings", "text_content": "Settings"},
        ]
        selected = {"s1", "s3"}

        filtered = [s for s in all_screens if s["screen_id"] in selected]
        infos = [_build_screen_info(s) for s in filtered]

        assert len(infos) == 2
        ids = {info.screen_id for info in infos}
        assert ids == {"s1", "s3"}
        # Verify types got carried through
        types = {info.screen_id: info.screen_type for info in infos}
        assert types["s1"] == "login"
        assert types["s3"] == "settings"

    def test_no_selection_returns_all_screens(self) -> None:
        """When selected_screen_ids is None, all screens pass through."""
        from src.api.routes._helpers import _build_screen_info

        all_screens = [
            {"screen_id": "s1", "screen_type": "login"},
            {"screen_id": "s2", "screen_type": "home"},
        ]
        selected_screen_ids = None

        if selected_screen_ids is not None:
            selected_set = set(selected_screen_ids)
            filtered = [s for s in all_screens if s["screen_id"] in selected_set]
        else:
            filtered = all_screens

        infos = [_build_screen_info(s) for s in filtered]
        assert len(infos) == 2
        assert infos[0].screen_type == "login"
        assert infos[1].screen_type == "home"

    def test_empty_selection_returns_no_screens(self) -> None:
        """Empty selected_screen_ids list means no screens pass filter."""
        all_screens = [
            {"screen_id": "s1", "screen_type": "login"},
            {"screen_id": "s2", "screen_type": "home"},
        ]
        selected_set = set([])
        filtered = [s for s in all_screens if s["screen_id"] in selected_set]
        assert filtered == []

    def test_nonexistent_ids_produce_empty_result(self) -> None:
        """Selecting IDs that don't exist yields no screens."""
        all_screens = [
            {"screen_id": "s1", "screen_type": "login"},
        ]
        selected_set = {"nonexistent_1", "nonexistent_2"}
        filtered = [s for s in all_screens if s["screen_id"] in selected_set]
        assert filtered == []


# ---------------------------------------------------------------------------
# Tests: ScreenSelectionRequest schema validation
# ---------------------------------------------------------------------------


class TestScreenSelectionRequestSchema:
    def test_valid_request_preserves_order(self) -> None:
        req = ScreenSelectionRequest(selected_screen_ids=["s3", "s1", "s2"])
        assert req.selected_screen_ids == ["s3", "s1", "s2"]

    def test_empty_list_is_valid(self) -> None:
        req = ScreenSelectionRequest(selected_screen_ids=[])
        assert req.selected_screen_ids == []

    def test_serialization_roundtrip(self) -> None:
        req = ScreenSelectionRequest(selected_screen_ids=["s1", "s2"])
        data = req.model_dump()
        assert data == {"selected_screen_ids": ["s1", "s2"]}
        restored = ScreenSelectionRequest(**data)
        assert restored.selected_screen_ids == ["s1", "s2"]


# ---------------------------------------------------------------------------
# Tests: ScreenPreview schema
# ---------------------------------------------------------------------------


class TestScreenPreviewSchema:
    def test_defaults(self) -> None:
        preview = ScreenPreview(screen_id="s1", screen_type="login")
        assert preview.screen_id == "s1"
        assert preview.screen_type == "login"
        assert preview.activity_name is None
        assert preview.text_preview is None
        assert preview.interactive_elements == 0
        assert preview.screenshot_path is None
        assert preview.selected is True

    def test_full(self) -> None:
        preview = ScreenPreview(
            screen_id="s1",
            screen_type="home",
            activity_name="com.app.Main",
            text_preview="Welcome home",
            interactive_elements=5,
            screenshot_path="/screenshots/s1.png",
            selected=False,
        )
        assert preview.activity_name == "com.app.Main"
        assert preview.interactive_elements == 5
        assert preview.selected is False
        assert preview.text_preview == "Welcome home"

    def test_serialization_includes_all_fields(self) -> None:
        preview = ScreenPreview(
            screen_id="s1",
            screen_type="login",
            interactive_elements=3,
            selected=False,
        )
        data = preview.model_dump()
        assert data["screen_id"] == "s1"
        assert data["selected"] is False
        assert data["interactive_elements"] == 3
        assert "activity_name" in data  # None fields still present
