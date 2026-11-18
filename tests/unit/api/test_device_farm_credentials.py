"""Per-request BrowserStack credentials (w2v P5 tenant ``device_farm`` slot).

Contract: ``DeviceFarmConfig.browserstack_username`` / ``browserstack_access_key``
are both required when ``provider == "browserstack"`` and forbidden otherwise.
They travel route -> background task -> ``process_job`` -> ``get_device_farm``
IN MEMORY ONLY: never on the ``inspection_jobs`` row, never in a response,
never in a log line. A job without them fails with a "resubmit" error, not the
``BrowserStackFarm.__init__`` TypeError it used to die on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from w2v_common.asgi import register_error_envelope_handlers

from src.api.routes import inspections
from src.api.schemas import DeviceFarmConfig
from src.services.device_farm import DeviceFarmCredentials
from src.services.inspector_service import InspectorService, JobManagerProto
from tests.conftest import TENANT_APP, attach_test_tenant
from tests.unit.test_inspector_service import (
    _FakeAppAcquirer,
    _FakeAutomationClient,
    _FakeAutomationClientFactory,
    _FakeDiscoverScreens,
    _FakeJob,
    _FakeJobManager,
    _make_storage,
)

BS_USER = "bs-user-7f3a"
BS_KEY = "bs-access-key-d41d8cd98f00"  # pragma: allowlist secret

BROWSERSTACK_BODY: dict[str, Any] = {
    "app_path": "https://example.com/app.apk",
    "platform": "android",
    "device_farm": {
        "provider": "browserstack",
        "browserstack_username": BS_USER,
        "browserstack_access_key": BS_KEY,
    },
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestDeviceFarmConfigValidation:
    def test_browserstack_without_credentials_rejected(self) -> None:
        with pytest.raises(ValidationError, match="required when provider='browserstack'"):
            DeviceFarmConfig(provider="browserstack")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"browserstack_username": BS_USER},
            {"browserstack_access_key": SecretStr(BS_KEY)},
        ],
    )
    def test_browserstack_with_half_the_credentials_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValidationError, match="both required"):
            DeviceFarmConfig(provider="browserstack", **kwargs)

    @pytest.mark.parametrize("provider", ["local", "docker_android"])
    def test_credentials_forbidden_for_other_providers(self, provider: str) -> None:
        with pytest.raises(ValidationError, match="only allowed"):
            DeviceFarmConfig(
                provider=cast(Any, provider),
                browserstack_username=BS_USER,
                browserstack_access_key=SecretStr(BS_KEY),
            )

    def test_local_default_unchanged(self) -> None:
        cfg = DeviceFarmConfig()
        assert cfg.provider == "local"
        assert cfg.browserstack_username is None
        assert cfg.browserstack_access_key is None

    def test_access_key_masked_in_repr_str_and_json(self) -> None:
        cfg = DeviceFarmConfig(
            provider="browserstack",
            browserstack_username=BS_USER,
            browserstack_access_key=SecretStr(BS_KEY),
        )
        assert cfg.browserstack_access_key is not None
        assert cfg.browserstack_access_key.get_secret_value() == BS_KEY
        for rendered in (repr(cfg), str(cfg), cfg.model_dump_json()):
            assert BS_KEY not in rendered

    def test_credentials_dataclass_repr_hides_key(self) -> None:
        creds = DeviceFarmCredentials(username=BS_USER, access_key=SecretStr(BS_KEY))
        assert BS_KEY not in repr(creds)
        assert BS_KEY not in str(creds)


# ---------------------------------------------------------------------------
# Route (stubbed service, no DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_app() -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    app.include_router(inspections.router)
    # The production 422 handler (src/asgi.py). FastAPI's DEFAULT handler echoes
    # each error's ``input`` -- i.e. the submitted access key -- so these tests
    # must run against the handler production actually uses.
    register_error_envelope_handlers(app, debug=False)
    service = MagicMock()
    service.create_job = AsyncMock(return_value="00000000-0000-7000-8000-000000000001")
    app.state.inspector_service = service

    db_session = AsyncMock()

    async def _override_db() -> Any:
        yield db_session

    from src.database.session import get_db

    app.dependency_overrides[get_db] = _override_db
    return attach_test_tenant(app), service


async def _drain_background_tasks() -> None:
    """Let the route's fire-and-forget task run (other tests may leave mocks here)."""
    loop = asyncio.get_running_loop()
    tasks = [
        t
        for t in list(inspections._background_tasks)
        if isinstance(t, asyncio.Task) and t.get_loop() is loop
    ]
    await asyncio.gather(*tasks)


async def _post(app: FastAPI, body: dict) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post("/api/inspect", json=body)


class TestRouteCredentials:
    async def test_browserstack_without_credentials_is_422(
        self, stub_app: tuple[FastAPI, MagicMock]
    ) -> None:
        app, service = stub_app
        body = {
            "app_path": "https://example.com/app.apk",
            "device_farm": {"provider": "browserstack"},
        }
        resp = await _post(app, body)
        assert resp.status_code == 422, resp.text
        service.create_job.assert_not_awaited()

    @pytest.mark.parametrize(
        ("device_farm", "leak"),
        [
            # credentials on a non-browserstack provider
            (
                {
                    "provider": "local",
                    "browserstack_username": BS_USER,
                    "browserstack_access_key": BS_KEY,
                },
                BS_KEY,
            ),
            # key without username (model-level error carries the whole dict)
            ({"provider": "browserstack", "browserstack_access_key": BS_KEY}, BS_KEY),
            # over-long key (field-level error carries the raw value)
            (
                {
                    "provider": "browserstack",
                    "browserstack_username": BS_USER,
                    "browserstack_access_key": BS_KEY * 20,
                },
                BS_KEY,
            ),
        ],
        ids=["creds-on-local", "key-without-user", "key-too-long"],
    )
    async def test_rejected_credentials_are_422_and_never_echoed_or_logged(
        self,
        stub_app: tuple[FastAPI, MagicMock],
        caplog: pytest.LogCaptureFixture,
        device_farm: dict,
        leak: str,
    ) -> None:
        app, service = stub_app
        body = {"app_path": "https://example.com/app.apk", "device_farm": device_farm}
        with caplog.at_level(logging.DEBUG):
            resp = await _post(app, body)
        assert resp.status_code == 422, resp.text
        assert leak not in resp.text
        assert leak not in caplog.text
        service.create_job.assert_not_awaited()

    async def test_credentials_reach_background_task_but_not_job_or_response_or_logs(
        self, stub_app: tuple[FastAPI, MagicMock], caplog: pytest.LogCaptureFixture
    ) -> None:
        app, service = stub_app
        bg = AsyncMock()
        with (
            patch.object(inspections, "_process_inspection_bg", bg),
            caplog.at_level(logging.DEBUG),
        ):
            resp = await _post(app, BROWSERSTACK_BODY)
            await _drain_background_tasks()

        assert resp.status_code == 200, resp.text
        assert BS_KEY not in resp.text
        assert BS_USER not in resp.text

        # The job row gets the provider name only.
        create_kwargs = service.create_job.await_args.kwargs
        assert create_kwargs["device_farm_provider"] == "browserstack"
        assert BS_KEY not in repr(create_kwargs)
        assert BS_USER not in repr(create_kwargs)

        # The background task gets the credentials, in memory.
        bg.assert_called_once()
        creds = bg.call_args.kwargs["farm_credentials"]
        assert isinstance(creds, DeviceFarmCredentials)
        assert creds.username == BS_USER
        assert creds.access_key.get_secret_value() == BS_KEY

        assert BS_KEY not in caplog.text

    async def test_local_provider_passes_no_credentials(
        self, stub_app: tuple[FastAPI, MagicMock]
    ) -> None:
        app, service = stub_app
        bg = AsyncMock()
        with patch.object(inspections, "_process_inspection_bg", bg):
            resp = await _post(app, {"app_path": "https://example.com/app.apk"})
            await _drain_background_tasks()
        assert resp.status_code == 200, resp.text
        assert service.create_job.await_args.kwargs["device_farm_provider"] == "local"
        assert bg.call_args.kwargs["farm_credentials"] is None


# ---------------------------------------------------------------------------
# process_job -> real get_device_farm (BrowserStackFarm mocked)
# ---------------------------------------------------------------------------


class _RecordingFarm:
    name = "browserstack"

    async def upload_app(self, app_path: str) -> str:
        return "bs://app-id"

    async def get_appium_url(self) -> str:
        return "https://hub-cloud.browserstack.com/wd/hub"

    async def get_capabilities(self, spec: Any) -> dict:
        return {}


def _service_with_real_factory(job: _FakeJob) -> tuple[InspectorService, _FakeJobManager]:
    jm = _FakeJobManager(get_job_return=job)
    service = InspectorService(
        storage=_make_storage(),
        job_manager=cast(JobManagerProto, jm),
        app_acquirer_factory=lambda: _FakeAppAcquirer(),
        # device_farm_factory omitted -> the real get_device_farm
        discover_screens_fn=_FakeDiscoverScreens(),
        automation_client_factory=_FakeAutomationClientFactory(_FakeAutomationClient()),
    )
    return service, jm


class TestProcessJobFarmCredentials:
    async def test_browserstack_farm_receives_credentials(self) -> None:
        job = _FakeJob(job_id="job-bs", device_farm_provider="browserstack")
        service, jm = _service_with_real_factory(job)
        bs_cls = MagicMock(return_value=_RecordingFarm())

        with patch.dict("src.services.device_farm.FARM_PROVIDERS", {"browserstack": bs_cls}):
            await service.process_job(
                AsyncMock(),
                "job-bs",
                TENANT_APP,
                farm_credentials=DeviceFarmCredentials(
                    username=BS_USER, access_key=SecretStr(BS_KEY)
                ),
            )

        bs_cls.assert_called_once_with(username=BS_USER, access_key=BS_KEY)
        assert jm.mark_failed_calls == []
        assert len(jm.mark_completed_calls) == 1

    async def test_browserstack_without_credentials_fails_with_resubmit_error(self) -> None:
        """Creds are memory-only: a job that lost them must fail clearly, not TypeError."""
        job = _FakeJob(job_id="job-lost", device_farm_provider="browserstack")
        service, jm = _service_with_real_factory(job)
        bs_cls = MagicMock()

        with patch.dict("src.services.device_farm.FARM_PROVIDERS", {"browserstack": bs_cls}):
            await service.process_job(AsyncMock(), "job-lost", TENANT_APP)

        bs_cls.assert_not_called()
        assert len(jm.mark_failed_calls) == 1
        error = jm.mark_failed_calls[0]["error"]
        assert "device farm credentials unavailable" in error
        assert "resubmit" in error
        assert "TypeError" not in error
        assert "__init__" not in error

    async def test_local_provider_unchanged(self) -> None:
        job = _FakeJob(job_id="job-local", device_farm_provider="local")
        service, jm = _service_with_real_factory(job)
        local_cls = MagicMock(return_value=_RecordingFarm())

        with patch.dict("src.services.device_farm.FARM_PROVIDERS", {"local": local_cls}):
            await service.process_job(AsyncMock(), "job-local", TENANT_APP)

        local_cls.assert_called_once_with()
        assert jm.mark_failed_calls == []


# ---------------------------------------------------------------------------
# Real postgres: nothing credential-shaped lands on the row or in GET
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_client(db_session, real_redis_client, monkeypatch):
    monkeypatch.setattr("src.api.routes.jobs.get_redis_client", lambda: real_redis_client)

    from src.api.routes import router as api_router
    from src.database.session import get_db

    app = FastAPI(redirect_slashes=False)
    app.include_router(api_router)
    app.state.inspector_service = InspectorService(storage=_make_storage())

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=attach_test_tenant(app)),
        base_url="http://testserver",
    ) as client:
        yield client


class TestCredentialsNeverPersisted:
    async def test_job_row_and_status_response_carry_no_credentials(
        self, real_client: httpx.AsyncClient, db_session
    ) -> None:
        bg = AsyncMock()
        with patch.object(inspections, "_process_inspection_bg", bg):
            resp = await real_client.post("/api/inspect", json=BROWSERSTACK_BODY)
            await _drain_background_tasks()
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]

        row = (
            await db_session.execute(
                text("SELECT row_to_json(j)::text FROM inspection_jobs j WHERE job_id = :id"),
                {"id": job_id},
            )
        ).scalar_one()
        assert '"device_farm_provider":"browserstack"' in row
        assert BS_KEY not in row
        assert BS_USER not in row

        status = await real_client.get(f"/api/inspect/{job_id}")
        assert status.status_code == 200, status.text
        assert BS_KEY not in status.text
        assert BS_USER not in status.text
