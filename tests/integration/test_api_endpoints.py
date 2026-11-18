"""Integration tests for all API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import redis


class TestRootEndpoint:
    def test_root_returns_service_info(self, test_client):
        response = test_client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "App Inspector Service"
        assert "version" in data
        assert data["status"] == "running"


class TestHealthEndpoints:
    def test_health_live(self, test_client):
        response = test_client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestInspectionEndpoints:
    def test_start_inspection(self, test_client):
        with (
            patch(
                "src.api.routes.inspections._get_tenant_id",
                return_value="01950000-0000-7000-8000-00000000ffff",
            ),
            patch("src.api.routes.inspections._process_inspection_bg"),
            patch("src.api.routes.inspections.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()

            # The real ``_validate_app_path`` runs here (no passthrough stub):
            # this path is inside the default allowed local dirs.
            response = test_client.post(
                "/api/inspect",
                json={
                    "app_path": "/tmp/app_inspector/test.apk",
                    "platform": "android",
                    "max_screens": 10,
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["job_id"] == "test-job-123"
            assert data["status"] == "pending"
            assert "poll_url" in data

    def test_start_inspection_rejects_path_outside_allowed_dirs(self, test_client):
        """The real ``_validate_app_path`` must run on the live route.

        Kills two mutations: (1) deleting/short-circuiting the
        ``_validate_app_path(...)`` call in ``start_inspection`` — the request
        would then 200 and create a job for ``/etc/passwd``; (2) re-stubbing
        the validator as a passthrough, which this test deliberately does not
        do. Paired with ``test_start_inspection`` (a legitimate in-allowed-dir
        path that must still 200), so the guard cannot be made unconditional
        either.
        """
        with (
            patch(
                "src.api.routes.inspections._get_tenant_id",
                return_value="01950000-0000-7000-8000-00000000ffff",
            ),
            patch("src.api.routes.inspections._process_inspection_bg"),
            patch("src.api.routes.inspections.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()

            response = test_client.post(
                "/api/inspect",
                json={"app_path": "/etc/passwd", "platform": "android"},
            )

            assert response.status_code == 400, response.text
            # No job may be created for a rejected path.
            test_client.app.state.inspector_service.create_job.assert_not_awaited()

    def test_start_inspection_no_redirect_on_canonical_path(self, test_client):
        """POST /api/inspect (no trailing slash) MUST not 307-redirect.

        Regression for the silent-callback breakage where FastAPI's default
        ``redirect_slashes=True`` turned ``POST /api/inspect`` into a 307 to
        ``/api/inspect/``. ``BaseServiceClient`` does not follow redirects,
        so callers (orchestrator, n8n nodes) saw an opaque failure.

        We assert the raw transport response — ``follow_redirects=False`` —
        so a regression cannot hide behind TestClient's default redirect
        following.
        """
        with (
            patch(
                "src.api.routes.inspections._get_tenant_id",
                return_value="01950000-0000-7000-8000-00000000ffff",
            ),
            patch("src.api.routes.inspections._process_inspection_bg"),
            patch("src.api.routes.inspections.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()

            response = test_client.post(
                "/api/inspect",
                json={
                    "app_path": "/tmp/app_inspector/test.apk",
                    "platform": "android",
                    "max_screens": 10,
                },
                follow_redirects=False,
            )
            # Must be a direct 200 — never a 307/308 redirect to a slashed
            # variant, which would silently break BaseServiceClient callers.
            assert response.status_code == 200, (
                f"Expected 200 on canonical path /api/inspect, got "
                f"{response.status_code}. Header location="
                f"{response.headers.get('location')!r}"
            )
            assert "location" not in {k.lower() for k in response.headers}

    def test_get_job_status(self, test_client, mock_job):
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
            assert data["job_id"] == "test-job-123"
            assert data["status"] == "completed"

    def test_get_job_status_not_found(self, test_client):
        with (
            patch(
                "src.api.routes.jobs.JobManager.get_job",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("src.api.routes.jobs.get_redis_client", side_effect=redis.RedisError("no redis")),
        ):
            response = test_client.get("/api/inspect/nonexistent")
            assert response.status_code == 404

    def test_get_inspection_result(
        self, test_client, mock_job, sample_screens_data, sample_nav_graph
    ):
        mock_job.screens_data = sample_screens_data
        mock_job.navigation_graph = sample_nav_graph
        mock_job.selected_screen_ids = None

        with patch(
            "src.api.routes.jobs.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/result")
            assert response.status_code == 200
            data = response.json()
            assert data["job_id"] == "test-job-123"
            assert len(data["screens"]) == 2
            assert len(data["navigation_graph"]) == 1
            # Regression: the /result builder must SURFACE the computed
            # estimated_purpose. It is inferred + persisted on the job
            # (inspector_service.infer_purpose_heuristic → mark_completed) but
            # was silently dropped from this response, so every downstream
            # consumer (orchestrator app_review) read None. See HANDOFF
            # app-inspector-fix.
            assert data["estimated_purpose"] == mock_job.estimated_purpose
            assert data["estimated_purpose"]  # non-empty on a completed job

    def test_get_inspection_result_not_completed(self, test_client, mock_job):
        mock_job.status = "processing"
        with patch(
            "src.api.routes.jobs.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/result")
            assert response.status_code == 409

    def test_list_screens(self, test_client, mock_job, sample_screens_data):
        mock_job.screens_data = sample_screens_data
        with patch(
            "src.api.routes.jobs.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/screens")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2


class TestFarmEndpoints:
    def test_list_farms(self, test_client):
        response = test_client.get("/api/inspect/farms")
        assert response.status_code == 200
        data = response.json()
        assert "farms" in data
        farm_names = [f["name"] for f in data["farms"]]
        assert "local" in farm_names

    def test_farm_devices_unknown_provider_returns_404(self, test_client):
        response = test_client.get("/api/inspect/farms/nonexistent/devices")
        assert response.status_code == 404
        data = response.json()
        assert "Unknown farm provider" in data["error"]["message"]

    def test_farm_devices_unconfigured_provider_returns_404(self, test_client):
        """BrowserStack is registered but requires credentials; calling without them returns 404."""
        response = test_client.get("/api/inspect/farms/browserstack/devices")
        assert response.status_code == 404
        data = response.json()
        assert data["error"]["message"] == "Provider not found: browserstack"


class TestComparisonEndpoint:
    def test_compare_inspections(
        self, test_client, mock_job, sample_screens_data, sample_nav_graph
    ):
        mock_job.screens_data = sample_screens_data
        mock_job.navigation_graph = sample_nav_graph

        with patch(
            "src.api.routes.compare.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.post(
                "/api/inspect/compare",
                json={
                    "baseline_job_id": "job-1",
                    "comparison_job_id": "job-2",
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert "summary" in data


class TestExportEndpoints:
    def test_export_html(self, test_client, mock_job, sample_screens_data):
        mock_job.screens_data = sample_screens_data
        mock_job.navigation_graph = []
        mock_job.selected_screen_ids = None

        with patch(
            "src.api.routes.export.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/export/html")
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "Inspection Report" in response.text

    def test_export_xml(self, test_client, mock_job, sample_screens_data):
        mock_job.screens_data = sample_screens_data
        mock_job.navigation_graph = []
        mock_job.selected_screen_ids = None

        with patch(
            "src.api.routes.export.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/export/xml")
            assert response.status_code == 200
            assert "application/xml" in response.headers["content-type"]

    def test_export_html_escapes_the_inspected_apps_own_text(self, test_client, mock_job):
        """End-to-end over the wire: app UI text is data, never markup.

        ``text_content`` is the INSPECTED APP's own screen text (xml_parser
        ``all_text`` -> screen_navigator -> screens_data), and this route
        returns it as ``text/html`` from our origin. Inspect a hostile app and
        the report must still be inert.
        """
        payload = "<script>alert(1)</script>"
        mock_job.screens_data = [
            {
                "screen_id": "s1",
                "screen_type": "content",
                "activity_name": "MainActivity",
                "text_content": f"Welcome {payload}",
                "interactive_elements": 1,
                "accessibility_score": 90,
                "accessibility_issues": [],
            }
        ]
        mock_job.navigation_graph = []
        mock_job.selected_screen_ids = None

        with patch(
            "src.api.routes.export.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/export/html")
            assert response.status_code == 200
            # Reached the renderer (otherwise this asserts nothing)...
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
            # ...and is not executable.
            assert payload not in response.text
            # Defence in depth: the report ships its own restrictive CSP.
            csp = response.headers["content-security-policy"]
            assert "default-src 'none'" in csp
            assert "sandbox" in csp
            assert "unsafe-inline" not in csp
            assert response.headers["x-content-type-options"] == "nosniff"

    def test_export_unsupported_format(self, test_client, mock_job):
        mock_job.screens_data = []
        mock_job.selected_screen_ids = None
        mock_job.navigation_graph = []

        with patch(
            "src.api.routes.export.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/export/pdf")
            assert response.status_code == 400


class TestScreenSelectionEndpoints:
    def test_screens_preview(self, test_client, mock_job, sample_screens_data):
        mock_job.screens_data = sample_screens_data
        mock_job.selected_screen_ids = None

        with patch(
            "src.api.routes.selection.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.get("/api/inspect/test-job-123/screens/preview")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
            assert data[0]["screen_id"] == "screen_0"

    def test_select_screens(self, test_client, mock_job, sample_screens_data):
        mock_job.screens_data = sample_screens_data
        mock_job.status = "awaiting_selection"

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
            data = response.json()
            assert data["selected_count"] == 1

    def test_select_invalid_screen_ids(self, test_client, mock_job, sample_screens_data):
        mock_job.screens_data = sample_screens_data
        mock_job.status = "completed"

        with patch(
            "src.api.routes.selection.JobManager.get_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ):
            response = test_client.post(
                "/api/inspect/test-job-123/screens/select",
                json={"selected_screen_ids": ["nonexistent"]},
            )
            assert response.status_code == 400
