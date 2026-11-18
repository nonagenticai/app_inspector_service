"""Unit tests for InspectorService -- orchestrator logic.

Uses Protocol-conforming fakes injected into ``InspectorService`` instead
of ``mock.patch``-ing the SUT module. Tests assert observable behavior
(returned values + recorded fake state), not "mock was called".
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from selenium.common.exceptions import WebDriverException

from src.services.automation_client import AutomationClient
from src.services.inspector_service import (
    InspectorService,
    JobManagerProto,
    _detect_platform,
)
from src.services.screen_navigator import DiscoveredScreen, NavigationEdge

# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


@dataclass
class _FakeJob:
    """Minimal stand-in for ``InspectionJob`` ORM rows."""

    job_id: str = "job-0"
    # `_acquire_app` passes the job's tenant down to `confine_minio_uri`, so the
    # double has to carry one or every process-job test dies on AttributeError.
    tenant_id: str = "01950000-0000-7000-8000-000000000000"
    platform: str | None = "android"
    device_farm_provider: str = "local"
    app_path: str | None = "/tmp/app.apk"
    automation_backend: str = "appium"
    max_screens: int = 10
    enable_recording: bool = False
    require_selection: bool = False


class _FakeJobManager:
    """Fake ``JobManagerProto``: records calls, returns canned responses."""

    def __init__(self, get_job_return: _FakeJob | None = None) -> None:
        self._get_job_return = get_job_return
        self.created_jobs: list[dict] = []
        self.processing_calls: list[str] = []
        self.mark_completed_calls: list[dict] = []
        self.mark_awaiting_selection_calls: list[dict] = []
        self.mark_failed_calls: list[dict] = []
        self.usage_calls: list[dict] = []

    async def create_job(self, db: Any, **kwargs: Any) -> Any:
        self.created_jobs.append(kwargs)
        # Return a job-like object whose job_id reflects the input
        return _FakeJob(
            job_id=f"job-{len(self.created_jobs)}",
            **{
                k: v
                for k, v in kwargs.items()
                if k
                in {
                    "platform",
                    "app_path",
                    "automation_backend",
                    "device_farm_provider",
                    "max_screens",
                    "enable_recording",
                    "require_selection",
                }
            },
        )

    async def get_job(self, db: Any, job_id: str, *, tenant_id: str | None = None) -> Any:
        if self._get_job_return is None:
            return None
        return self._get_job_return

    async def mark_processing(self, db: Any, job_id: str) -> None:
        self.processing_calls.append(job_id)

    async def mark_completed(self, db: Any, job_id: str, **kwargs: Any) -> None:
        self.mark_completed_calls.append({"job_id": job_id, **kwargs})

    async def mark_awaiting_selection(self, db: Any, job_id: str, **kwargs: Any) -> None:
        self.mark_awaiting_selection_calls.append({"job_id": job_id, **kwargs})

    async def mark_failed_with_partial(
        self, db: Any, job_id: str, error: str, **kwargs: Any
    ) -> None:
        self.mark_failed_calls.append({"job_id": job_id, "error": error, **kwargs})

    async def record_usage(self, db: Any, **kwargs: Any) -> None:
        self.usage_calls.append(kwargs)


@dataclass
class _FakeAcquired:
    local_path: str
    platform: str | None = None
    # Mirrors AcquiredApp.is_temp: True only for tempfiles acquisition owns
    # (URL/MinIO/store downloads), False for a user-supplied local path.
    is_temp: bool = False


class _FakeAppAcquirer:
    def __init__(
        self,
        local_path: str = "/tmp/app.apk",
        learned_platform: str | None = None,
        side_effect: Exception | None = None,
        is_temp: bool = False,
    ) -> None:
        self._local_path = local_path
        self._platform = learned_platform
        self._side_effect = side_effect
        self._is_temp = is_temp
        self.acquire_calls: list[str] = []
        self.acquire_tenants: list[str | None] = []

    async def acquire(self, app_path: str, /, *, tenant_id: str | None = None) -> Any:
        self.acquire_calls.append(app_path)
        self.acquire_tenants.append(tenant_id)
        if self._side_effect is not None:
            raise self._side_effect
        return _FakeAcquired(
            local_path=self._local_path, platform=self._platform, is_temp=self._is_temp
        )


class _FakeFarm:
    def __init__(self) -> None:
        self.name = "fake"
        self.uploaded: list[str] = []
        self.capabilities_for: list[str] = []
        self.factory_calls: list[tuple[str, dict[str, str]]] = []

    async def upload_app(self, app_path: str) -> str:
        self.uploaded.append(app_path)
        return app_path

    async def get_appium_url(self) -> str:
        return "http://localhost:4723"

    async def get_capabilities(self, spec: Any) -> dict:
        self.capabilities_for.append(spec.platform)
        return {"capability": "test"}


def _fake_device_farm_factory(farm: _FakeFarm) -> Any:
    def _factory(provider: str, /, **kwargs: str) -> Any:
        farm.factory_calls.append((provider, kwargs))
        return farm

    return _factory


class _FakeAutomationClient:
    def __init__(
        self,
        *,
        stop_recording_returns: bytes | None = None,
        connect_side_effect: Exception | None = None,
    ) -> None:
        self.connected_to: str | None = None
        self.disconnect_called = False
        self.start_recording_called = False
        self.stop_recording_called = False
        self._stop_returns = stop_recording_returns
        self._connect_side_effect = connect_side_effect

    async def connect(self, app_url: str) -> None:
        if self._connect_side_effect is not None:
            raise self._connect_side_effect
        self.connected_to = app_url

    async def disconnect(self) -> None:
        self.disconnect_called = True

    async def start_recording(self) -> None:
        self.start_recording_called = True

    async def stop_recording(self) -> bytes | None:
        self.stop_recording_called = True
        return self._stop_returns

    # The following are unused by InspectorService directly but required by
    # AutomationClient ABC at runtime if subclasses need them; AsyncMock-style
    # stand-in keeps tests free of NotImplementedError.
    async def get_page_source(self) -> str:
        return "<hierarchy/>"

    async def find_clickable_elements(self) -> list[dict]:
        return []

    async def tap(self, x: int, y: int) -> None: ...

    async def back(self) -> None: ...

    async def screenshot(self, path: str) -> None: ...


class _FakeAutomationClientFactory:
    def __init__(self, client: _FakeAutomationClient) -> None:
        self.client = client
        self.calls: list[dict] = []

    def __call__(self, *, backend: str, platform: str, appium_url: str, capabilities: dict) -> Any:
        self.calls.append(
            {
                "backend": backend,
                "platform": platform,
                "appium_url": appium_url,
                "capabilities": capabilities,
            }
        )
        return self.client


@dataclass
class _DiscoverResult:
    screens: list[DiscoveredScreen] = field(default_factory=list)
    edges: list[NavigationEdge] = field(default_factory=list)


class _FakeDiscoverScreens:
    def __init__(self, result: _DiscoverResult | None = None) -> None:
        self.result = result or _DiscoverResult()
        self.calls: list[dict] = []

    async def __call__(
        self,
        client: AutomationClient,
        max_screens: int = 25,
        screenshot_dir: str | None = None,
        on_screen_discovered: Any = None,
    ) -> tuple[list[DiscoveredScreen], list[NavigationEdge]]:
        self.calls.append(
            {
                "max_screens": max_screens,
                "screenshot_dir": screenshot_dir,
            }
        )
        return self.result.screens, self.result.edges


def _build_service(
    *,
    job_manager: _FakeJobManager,
    acquirer: _FakeAppAcquirer | None = None,
    farm: _FakeFarm | None = None,
    discover: _FakeDiscoverScreens | None = None,
    client_factory: _FakeAutomationClientFactory | None = None,
    storage: Any = None,
) -> tuple[
    InspectorService,
    _FakeJobManager,
    _FakeAppAcquirer,
    _FakeFarm,
    _FakeDiscoverScreens,
    _FakeAutomationClientFactory,
]:
    acquirer = acquirer or _FakeAppAcquirer()
    farm = farm or _FakeFarm()
    discover = discover or _FakeDiscoverScreens()
    client_factory = client_factory or _FakeAutomationClientFactory(_FakeAutomationClient())
    storage = storage or _make_storage()

    service = InspectorService(
        storage=storage,
        job_manager=cast(JobManagerProto, job_manager),
        app_acquirer_factory=lambda: acquirer,
        device_farm_factory=_fake_device_farm_factory(farm),
        discover_screens_fn=discover,
        automation_client_factory=client_factory,
    )
    return service, job_manager, acquirer, farm, discover, client_factory


def _make_storage(video_path: str | None = "/tmp/video.mp4", job_dir: Path | None = None) -> Any:
    storage = MagicMock()
    storage.backend = "local"  # _persist_screenshots no-ops for local
    storage.get_screenshots_dir = MagicMock(return_value="/tmp/screenshots")
    storage.get_job_dir = MagicMock(return_value=job_dir if job_dir is not None else Path("/tmp"))
    if video_path is not None:
        storage.store_recording = AsyncMock(return_value=video_path)
    return storage


# ============================================================================
# _detect_platform
# ============================================================================


class TestDetectPlatform:
    def test_apk_is_android(self):
        assert _detect_platform("app.apk") == "android"

    def test_ipa_is_ios(self):
        assert _detect_platform("app.ipa") == "ios"

    def test_dot_app_is_ios(self):
        assert _detect_platform("/path/to/MyApp.app") == "ios"

    def test_uppercase_apk(self):
        assert _detect_platform("MyApp.APK") == "android"

    def test_unknown_defaults_to_android(self):
        assert _detect_platform("somefile.zip") == "android"

    def test_no_extension_defaults_to_android(self):
        assert _detect_platform("somefile") == "android"

    def test_mixed_case_ipa(self):
        assert _detect_platform("App.IPA") == "ios"

    def test_path_with_dots_in_directory(self):
        assert _detect_platform("/path.to/dir/app.ipa") == "ios"


# ============================================================================
# InspectorService.create_job
# ============================================================================


class TestCreateJob:
    async def test_create_job_auto_detects_ios_platform(self):
        jm = _FakeJobManager()
        service, *_ = _build_service(job_manager=jm)
        db = AsyncMock()

        job_id = await service.create_job(
            db,
            tenant_id="01950000-0000-7000-8000-000000000001",
            app_path="/tmp/app.ipa",
            platform=None,
        )

        assert job_id == "job-1"
        assert len(jm.created_jobs) == 1
        kwargs = jm.created_jobs[0]
        assert kwargs["platform"] == "ios"
        assert kwargs["tenant_id"] == "01950000-0000-7000-8000-000000000001"
        assert kwargs["app_path"] == "/tmp/app.ipa"

    async def test_create_job_explicit_platform_honored(self):
        jm = _FakeJobManager()
        service, *_ = _build_service(job_manager=jm)
        db = AsyncMock()

        await service.create_job(
            db,
            tenant_id="01950000-0000-7000-8000-000000000001",
            app_path="/tmp/app.apk",
            platform="android",
        )

        kwargs = jm.created_jobs[0]
        assert kwargs["platform"] == "android"

    async def test_create_job_passes_all_options(self):
        jm = _FakeJobManager()
        service, *_ = _build_service(job_manager=jm)
        db = AsyncMock()

        await service.create_job(
            db,
            tenant_id="01950000-0000-7000-8000-000000000001",
            app_path="/tmp/app.apk",
            enable_recording=True,
            require_selection=True,
            automation_backend="maestro",
            device_farm_provider="browserstack",
            max_screens=50,
        )

        kwargs = jm.created_jobs[0]
        assert kwargs["enable_recording"] is True
        assert kwargs["require_selection"] is True
        assert kwargs["automation_backend"] == "maestro"
        assert kwargs["device_farm_provider"] == "browserstack"
        assert kwargs["max_screens"] == 50


# ============================================================================
# InspectorService.process_job
# ============================================================================


class TestProcessJob:
    async def test_process_job_happy_path_passes_correct_data_to_mark_completed(self):
        job = _FakeJob(job_id="job-1")
        screen = DiscoveredScreen(
            screen_id="screen_0",
            screen_type="home",
            xml_hash="h0",
            text_content="Welcome",
        )
        edge = NavigationEdge(from_screen="screen_0", to_screen="screen_1", element_label="Login")

        jm = _FakeJobManager(get_job_return=job)
        discover = _FakeDiscoverScreens(_DiscoverResult(screens=[screen], edges=[edge]))
        client = _FakeAutomationClient()
        client_factory = _FakeAutomationClientFactory(client)

        service, *_ = _build_service(
            job_manager=jm,
            discover=discover,
            client_factory=client_factory,
        )
        db = AsyncMock()

        await service.process_job(db, "job-1", "01950000-0000-7000-8000-000000000001")

        # mark_completed received the actual screen / edge data
        assert len(jm.mark_completed_calls) == 1
        call = jm.mark_completed_calls[0]
        assert call["job_id"] == "job-1"
        assert call["screens_discovered"] == 1
        assert len(call["screens_data"]) == 1
        assert call["screens_data"][0]["screen_id"] == "screen_0"
        assert call["screens_data"][0]["screen_type"] == "home"
        assert call["screens_data"][0]["text_content"] == "Welcome"
        assert len(call["navigation_graph"]) == 1
        assert call["navigation_graph"][0]["from_screen"] == "screen_0"
        assert call["navigation_graph"][0]["to_screen"] == "screen_1"
        assert call["navigation_graph"][0]["element_label"] == "Login"
        assert call["inspection_time_seconds"] >= 0
        assert isinstance(call["estimated_purpose"], str)

        # record_usage with correct counts
        assert len(jm.usage_calls) == 1
        assert jm.usage_calls[0]["tenant_id"] == "01950000-0000-7000-8000-000000000001"
        assert jm.usage_calls[0]["inspections_completed"] == 1
        assert jm.usage_calls[0]["screens_discovered"] == 1

        # client lifecycle
        assert client.connected_to is not None
        assert client.disconnect_called is True

    async def test_process_job_maestro_uses_maestro_backend(self):
        job = _FakeJob(job_id="job-2", automation_backend="maestro", max_screens=5)
        jm = _FakeJobManager(get_job_return=job)
        screen = DiscoveredScreen(screen_id="screen_0", screen_type="home", xml_hash="h0")
        discover = _FakeDiscoverScreens(_DiscoverResult(screens=[screen]))
        client = _FakeAutomationClient()
        client_factory = _FakeAutomationClientFactory(client)

        service, *_ = _build_service(
            job_manager=jm, discover=discover, client_factory=client_factory
        )
        db = AsyncMock()

        await service.process_job(db, "job-2", "01950000-0000-7000-8000-000000000001")

        # Factory was invoked with backend=maestro
        assert len(client_factory.calls) == 1
        assert client_factory.calls[0]["backend"] == "maestro"
        assert client_factory.calls[0]["platform"] == "android"
        # Connection still occurred
        assert client.connected_to is not None

    async def test_process_job_not_found_raises_with_job_id(self):
        jm = _FakeJobManager(get_job_return=None)
        service, *_ = _build_service(job_manager=jm)
        db = AsyncMock()

        with pytest.raises(ValueError, match="Job not found") as exc_info:
            await service.process_job(db, "nonexistent", "01950000-0000-7000-8000-000000000001")
        assert "nonexistent" in str(exc_info.value)

    async def test_process_job_failure_marks_failed_with_error_message(self):
        job = _FakeJob(job_id="job-fail")
        acquirer = _FakeAppAcquirer(side_effect=OSError("Download failed"))
        jm = _FakeJobManager(get_job_return=job)

        service, *_ = _build_service(job_manager=jm, acquirer=acquirer)
        db = AsyncMock()

        await service.process_job(db, "job-fail", "01950000-0000-7000-8000-000000000001")

        # mark_failed_with_partial received the actual error string
        assert len(jm.mark_failed_calls) == 1
        assert jm.mark_failed_calls[0]["job_id"] == "job-fail"
        assert "Download failed" in jm.mark_failed_calls[0]["error"]

        # usage recorded as error
        assert len(jm.usage_calls) == 1
        assert jm.usage_calls[0]["is_error"] is True
        assert jm.usage_calls[0]["tenant_id"] == "01950000-0000-7000-8000-000000000001"

        # mark_completed should NOT have been called
        assert jm.mark_completed_calls == []

        # db.commit was awaited
        db.commit.assert_awaited()

    async def test_process_job_webdriver_exception_marks_failed(self):
        # A routine Appium/Selenium driver failure (session crash, UIAutomator2
        # timeout, hub unreachable) surfaces as WebDriverException from a call
        # OUTSIDE the per-element try (here: client.connect). It must route to
        # _handle_failure and mark the job FAILED, not escape process_job and
        # leave the job stuck in `processing` until the reaper.
        job = _FakeJob(job_id="job-wd")
        jm = _FakeJobManager(get_job_return=job)
        client = _FakeAutomationClient(connect_side_effect=WebDriverException("session crashed"))
        client_factory = _FakeAutomationClientFactory(client)

        service, *_ = _build_service(job_manager=jm, client_factory=client_factory)
        db = AsyncMock()

        # Must NOT propagate — handled and marked failed.
        await service.process_job(db, "job-wd", "01950000-0000-7000-8000-000000000001")

        assert len(jm.mark_failed_calls) == 1
        assert jm.mark_failed_calls[0]["job_id"] == "job-wd"
        assert "session crashed" in jm.mark_failed_calls[0]["error"]
        # job was never completed, and error usage was recorded
        assert jm.mark_completed_calls == []
        assert len(jm.usage_calls) == 1
        assert jm.usage_calls[0]["is_error"] is True

    async def test_process_job_with_recording_starts_and_stops(self, tmp_path: Path):
        job = _FakeJob(job_id="job-rec", enable_recording=True, max_screens=5)
        jm = _FakeJobManager(get_job_return=job)
        screen = DiscoveredScreen(screen_id="screen_0", screen_type="home", xml_hash="h0")
        discover = _FakeDiscoverScreens(_DiscoverResult(screens=[screen]))
        client = _FakeAutomationClient(stop_recording_returns=b"video-data")
        client_factory = _FakeAutomationClientFactory(client)
        # store_recording returns the shared-storage ref persisted on the job.
        storage = _make_storage(video_path="job-rec/recording.mp4", job_dir=tmp_path)

        service, *_ = _build_service(
            job_manager=jm,
            discover=discover,
            client_factory=client_factory,
            storage=storage,
        )
        db = AsyncMock()

        await service.process_job(db, "job-rec", "01950000-0000-7000-8000-000000000001")

        assert client.start_recording_called is True
        assert client.stop_recording_called is True
        # Recording was captured to the local scratch dir, then handed to the
        # streaming uploader (store_recording), NOT buffered via an in-memory put.
        assert (tmp_path / "recording.mp4").read_bytes() == b"video-data"
        storage.store_recording.assert_awaited_once()
        call_args = storage.store_recording.await_args.args
        assert call_args[0] == "job-rec"
        assert call_args[1] == tmp_path / "recording.mp4"
        # mark_completed received the shared-storage ref returned by store_recording.
        assert jm.mark_completed_calls[0]["video_path"] == "job-rec/recording.mp4"

    async def test_process_job_deletes_downloaded_tempfile_on_success(self, tmp_path: Path):
        # FIX 1 (temp artifact disk leak → self-DoS): the downloaded APK created
        # by app_acquisition via tempfile.mkstemp (URL/MinIO/store paths,
        # is_temp=True) is consumed by farm.upload_app but was NEVER deleted on
        # the success path — only on download failures. Every successful
        # inspection leaked a ≤500MB file until disk filled and all new
        # mkstemp/write raised OSError. process_job must unlink the OWNED
        # tempfile in its finally so it is gone on success (and failure).
        artifact = tmp_path / "downloaded.apk"
        artifact.write_bytes(b"PK\x03\x04fake-apk-payload")
        assert artifact.exists()

        job = _FakeJob(job_id="job-leak")
        jm = _FakeJobManager(get_job_return=job)
        acquirer = _FakeAppAcquirer(local_path=str(artifact), is_temp=True)
        screen = DiscoveredScreen(screen_id="screen_0", screen_type="home", xml_hash="h0")
        discover = _FakeDiscoverScreens(_DiscoverResult(screens=[screen]))

        service, *_ = _build_service(job_manager=jm, acquirer=acquirer, discover=discover)
        db = AsyncMock()

        await service.process_job(db, "job-leak", "01950000-0000-7000-8000-000000000001")

        # Run completed successfully AND the downloaded tempfile was cleaned up.
        assert len(jm.mark_completed_calls) == 1
        assert not artifact.exists(), "downloaded APK tempfile leaked on the success path"

    async def test_process_job_preserves_local_source_file_on_success(self, tmp_path: Path):
        # FIX 1 hole: for a LOCAL-path input, acquisition `_from_local_path`
        # returns the USER'S ORIGINAL file (is_temp=False), not a tempfile it
        # owns. An unconditional unlink in the finally DELETES that source file
        # on a SUCCESSFUL inspection → data loss, and breaks retry/resume
        # ("Local file not found" next time) + concurrent jobs sharing the
        # input. process_job must delete ONLY when acquisition owns the file.
        source = tmp_path / "mounted_app.apk"
        source.write_bytes(b"PK\x03\x04user-supplied-apk")
        assert source.exists()

        job = _FakeJob(job_id="job-local")
        jm = _FakeJobManager(get_job_return=job)
        acquirer = _FakeAppAcquirer(local_path=str(source), is_temp=False)
        screen = DiscoveredScreen(screen_id="screen_0", screen_type="home", xml_hash="h0")
        discover = _FakeDiscoverScreens(_DiscoverResult(screens=[screen]))

        service, *_ = _build_service(job_manager=jm, acquirer=acquirer, discover=discover)
        db = AsyncMock()

        await service.process_job(db, "job-local", "01950000-0000-7000-8000-000000000001")

        # Run completed successfully AND the user's original file survives.
        assert len(jm.mark_completed_calls) == 1
        assert source.exists(), "user's original local APK was deleted (data loss) on success"

    async def test_process_job_acquisition_error_records_error_usage(self):
        # FIX 2 (error usage not recorded on common failures): _acquire_app raises
        # AppAcquisitionError (a plain Exception subclass), which was NOT in
        # process_job's except tuple, so it skipped _handle_failure — the ONLY
        # path that calls record_usage(is_error=True). Per-tenant error usage was
        # silently never incremented for bad/unreachable app URLs.
        from src.services.app_acquisition import AppAcquisitionError

        job = _FakeJob(job_id="job-acq")
        jm = _FakeJobManager(get_job_return=job)
        acquirer = _FakeAppAcquirer(side_effect=AppAcquisitionError("bad app url"))

        service, *_ = _build_service(job_manager=jm, acquirer=acquirer)
        db = AsyncMock()

        # RED: AppAcquisitionError escapes the except tuple and propagates; suppress
        # so the ASSERTION below is the failure point, not the raise. GREEN: handled.
        with contextlib.suppress(AppAcquisitionError):
            await service.process_job(db, "job-acq", "01950000-0000-7000-8000-000000000001")

        # error usage MUST be recorded for the tenant, and the job marked failed.
        assert len(jm.usage_calls) == 1
        assert jm.usage_calls[0]["is_error"] is True
        assert jm.usage_calls[0]["tenant_id"] == "01950000-0000-7000-8000-000000000001"
        assert len(jm.mark_failed_calls) == 1
        assert jm.mark_completed_calls == []

    async def test_process_job_require_selection_calls_mark_awaiting(self):
        job = _FakeJob(job_id="job-sel", require_selection=True, max_screens=5)
        jm = _FakeJobManager(get_job_return=job)
        screen = DiscoveredScreen(screen_id="screen_0", screen_type="home", xml_hash="h0")
        discover = _FakeDiscoverScreens(_DiscoverResult(screens=[screen]))

        service, *_ = _build_service(job_manager=jm, discover=discover)
        db = AsyncMock()

        await service.process_job(db, "job-sel", "01950000-0000-7000-8000-000000000001")

        # mark_completed must NOT be called; mark_awaiting_selection must be
        assert jm.mark_completed_calls == []
        assert len(jm.mark_awaiting_selection_calls) == 1
        call = jm.mark_awaiting_selection_calls[0]
        assert call["screens_discovered"] == 1
        assert call["screens_data"] is not None
        assert call["navigation_graph"] == []


# ============================================================================
# _persist_screenshots -- a pod-local path to a missing file must not survive
# ============================================================================


class TestPersistScreenshots:
    """Under MinIO, an unpersistable path 404s on every other replica.

    Leaving it in the row is the same defect class as returning a path for a
    capture that failed: a non-empty reference to an artifact nobody can read.
    """

    @staticmethod
    def _minio_storage(job_dir: Path) -> Any:
        storage = _make_storage(job_dir=job_dir)
        storage.backend = "minio"
        storage.store_bytes = AsyncMock(return_value="screenshots/screen_0.png")
        return storage

    async def test_missing_scratch_file_clears_the_reference(self, tmp_path: Path) -> None:
        service, *_ = _build_service(
            job_manager=_FakeJobManager(), storage=self._minio_storage(tmp_path)
        )
        screens_data = [
            {
                "screen_id": "screen_0",
                "screenshot_path": str(tmp_path / "gone.png"),
                "screenshot_status": "captured",
            }
        ]

        await service._persist_screenshots("job-0", screens_data)

        assert screens_data[0]["screenshot_path"] is None
        assert screens_data[0]["screenshot_status"] == "failed"

    async def test_present_scratch_file_is_uploaded_and_rewritten(self, tmp_path: Path) -> None:
        shot = tmp_path / "screen_0.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
        service, *_ = _build_service(
            job_manager=_FakeJobManager(), storage=self._minio_storage(tmp_path)
        )
        screens_data = [
            {
                "screen_id": "screen_0",
                "screenshot_path": str(shot),
                "screenshot_status": "captured",
            }
        ]

        await service._persist_screenshots("job-0", screens_data)

        assert screens_data[0]["screenshot_path"] == "screenshots/screen_0.png"
        assert screens_data[0]["screenshot_status"] == "captured"
