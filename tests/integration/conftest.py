"""Integration test fixtures with TestClient and mocked dependencies."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.constants import DEFAULT_MAX_SCREENS
from tests.conftest import TENANT_APP, asgi_tenant_wrapper


@pytest.fixture
def mock_job():
    """Create a mock InspectionJob ORM object."""
    job = MagicMock()
    job.job_id = "test-job-123"
    job.tenant_id = TENANT_APP
    job.status = "completed"
    job.platform = "android"
    job.app_path = "/tmp/test.apk"
    job.automation_backend = "appium"
    job.device_farm_provider = "local"
    job.max_screens = DEFAULT_MAX_SCREENS
    job.screens_discovered = 2
    job.screens_data = []
    job.navigation_graph = []
    job.error_message = None
    job.inspection_time_seconds = 45.2
    job.created_at = MagicMock()
    job.created_at.isoformat.return_value = "2026-02-23T00:00:00+00:00"
    job.completed_at = MagicMock()
    job.completed_at.isoformat.return_value = "2026-02-23T00:01:00+00:00"
    job.estimated_purpose = "A login-based application"
    job.video_path = None
    job.enable_recording = False
    job.selected_screen_ids = None
    job.require_selection = False
    return job


@pytest.fixture
def test_client(mock_job, sample_screens_data, sample_nav_graph):
    """Create a FastAPI TestClient with mocked dependencies."""
    from src.config import clear_settings_cache

    clear_settings_cache()

    # Patch database and Redis connections at lifespan level
    with (
        patch("src.asgi.get_engine", new_callable=AsyncMock),
        patch("src.asgi.init_redis", new_callable=AsyncMock) as mock_init_redis,
        patch("src.asgi.get_redis_client") as mock_get_redis,
        patch("src.asgi.close_redis", new_callable=AsyncMock),
        patch("src.asgi.close_engine", new_callable=AsyncMock),
        patch("src.database.session.get_session_maker", new_callable=AsyncMock),
    ):
        # Set up Redis mock
        redis_mock = AsyncMock()
        redis_mock.ping = AsyncMock(return_value=True)
        mock_init_redis.return_value = redis_mock
        mock_get_redis.return_value = redis_mock

        # Import the app (after env vars are set by autouse fixture)
        from src.asgi import app

        # Override the get_db dependency to provide a mock session
        from src.database.session import get_db

        db_session = AsyncMock()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db

        # Set up storage mock
        storage = MagicMock()
        storage.initialize = AsyncMock()
        storage.get_screenshots_dir = MagicMock(return_value="/tmp/test/screenshots")
        app.state.storage_service = storage

        # Set up inspector service mock
        from src.services.inspector_service import InspectorService

        inspector = MagicMock(spec=InspectorService)
        inspector.create_job = AsyncMock(return_value="test-job-123")
        app.state.inspector_service = inspector

        # AUTH_ENABLED is false in tests, so AuthMiddleware is not registered
        # and nothing binds request.state.tenant_id. Since w2v-common 0.67.0 the
        # routes 401 rather than substituting a sentinel tenant, so the stand-in
        # has to bind a real tenant uuid the way AuthMiddleware would.
        client = TestClient(asgi_tenant_wrapper(app))

        yield client

        # Cleanup
        app.dependency_overrides.clear()
        clear_settings_cache()
