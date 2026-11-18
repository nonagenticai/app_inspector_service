"""Integration tests for job lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import redis

from tests.conftest import TENANT_APP


class TestJobLifecycle:
    def test_create_and_get_status(self, test_client, mock_job):
        """Test creating a job and checking its status."""
        # Create job
        with (
            patch("src.api.routes.inspections._get_tenant_id", return_value=TENANT_APP),
            patch("src.api.routes.inspections._process_inspection_bg"),
            patch("src.api.routes.inspections.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            # NOT a passthrough stub of ``_validate_app_path``: the real
            # validator runs, and this path is genuinely inside the default
            # ``ACQUISITION_ALLOWED_LOCAL_PATHS``.
            response = test_client.post(
                "/api/inspect",
                json={"app_path": "/tmp/app_inspector/test.apk"},
            )
            assert response.status_code == 200
            job_id = response.json()["job_id"]

        # Get status
        mock_job.status = "pending"
        with (
            patch(
                "src.api.routes.jobs.JobManager.get_job",
                new_callable=AsyncMock,
                return_value=mock_job,
            ),
            patch("src.api.routes.jobs.get_redis_client", side_effect=redis.RedisError("no redis")),
        ):
            response = test_client.get(f"/api/inspect/{job_id}")
            assert response.status_code == 200
            assert response.json()["status"] == "pending"

    def test_completed_job_has_result_url(self, test_client, mock_job):
        """Test that completed jobs include a result URL."""
        mock_job.status = "completed"
        with (
            patch(
                "src.api.routes.jobs.JobManager.get_job",
                new_callable=AsyncMock,
                return_value=mock_job,
            ),
            patch("src.api.routes.jobs.get_redis_client", side_effect=redis.RedisError("no redis")),
        ):
            response = test_client.get("/api/inspect/test-job-123")
            assert response.status_code == 200
            data = response.json()
            assert data["result_url"] is not None
            assert "result" in data["result_url"]

    def test_failed_job(self, test_client, mock_job):
        """Test that failed jobs return error information."""
        mock_job.status = "failed"
        mock_job.error_message = "Connection refused"
        with (
            patch(
                "src.api.routes.jobs.JobManager.get_job",
                new_callable=AsyncMock,
                return_value=mock_job,
            ),
            patch("src.api.routes.jobs.get_redis_client", side_effect=redis.RedisError("no redis")),
        ):
            response = test_client.get("/api/inspect/test-job-123")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "failed"
            assert data["error"] == "Connection refused"


class TestJobWithScreenSelection:
    def test_awaiting_selection_lifecycle(self, test_client, mock_job, sample_screens_data):
        """Test the full selection lifecycle."""
        # Job in awaiting_selection state
        mock_job.status = "awaiting_selection"
        mock_job.screens_data = sample_screens_data
        mock_job.selected_screen_ids = None
        mock_job.navigation_graph = []

        # Should be able to get result even in awaiting_selection
        with patch(
            "src.api.routes.jobs.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/result")
            assert response.status_code == 200
            assert len(response.json()["screens"]) == 2

        # Select screens
        with (
            patch(
                "src.api.routes.selection.JobManager.get_job",
                new_callable=AsyncMock,
                return_value=mock_job,
            ),
            patch(
                "src.api.routes.selection.JobManager.update_screen_selection",
                new_callable=AsyncMock,
            ),
        ):
            response = test_client.post(
                "/api/inspect/test-job-123/screens/select",
                json={"selected_screen_ids": ["screen_0"]},
            )
            assert response.status_code == 200

        # After selection, result should filter
        mock_job.status = "completed"
        mock_job.selected_screen_ids = ["screen_0"]
        with patch(
            "src.api.routes.jobs.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/result")
            assert response.status_code == 200
            assert len(response.json()["screens"]) == 1
