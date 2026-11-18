"""Top-level orchestrator for mobile app inspection."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast

import httpx
from selenium.common.exceptions import WebDriverException
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import DEFAULT_MAX_SCREENS
from src.database.models import InspectionJob
from src.helpers.url_validation import SSRFError
from src.services.app_acquisition import (
    AcquiredApp,
    AppAcquisitionError,
    AppAcquisitionService,
)
from src.services.appium_client import AppiumClient
from src.services.automation_client import AutomationClient
from src.services.device_farm import (
    DeviceFarmClient,
    DeviceFarmCredentials,
    DeviceSpec,
    device_farm_kwargs,
    get_device_farm,
    redact_farm_error,
)
from src.services.job_manager import JobManager, JobStateConflictError
from src.services.screen_navigator import (
    DiscoveredScreen,
    NavigationEdge,
)
from src.services.screen_navigator import (
    discover_screens as _real_discover_screens,
)
from src.services.storage_service import StorageService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Collaborator Protocols (DI seams for testing)
# ---------------------------------------------------------------------------


class JobManagerProto(Protocol):
    """Protocol covering the ``JobManager`` static methods InspectorService uses.

    Declared as a runtime-checkable structural protocol with callable
    attributes so that the real ``JobManager`` class (whose namespace
    holds ``staticmethod``-bound callables) satisfies the protocol.
    """

    create_job: Callable[..., Awaitable[InspectionJob]]
    get_job: Callable[..., Awaitable[InspectionJob | None]]
    mark_processing: Callable[..., Awaitable[None]]
    mark_completed: Callable[..., Awaitable[None]]
    mark_awaiting_selection: Callable[..., Awaitable[None]]
    mark_failed_with_partial: Callable[..., Awaitable[None]]
    record_usage: Callable[..., Awaitable[None]]


class AppAcquirerProto(Protocol):
    """Acquires an app binary from a path/URL/store ref."""

    async def acquire(self, input_str: str, /, *, tenant_id: str | None = None) -> AcquiredApp: ...


class DeviceFarmFactoryProto(Protocol):
    """Returns a DeviceFarmClient for a given provider name."""

    def __call__(self, provider: str, /, **kwargs: str) -> DeviceFarmClient: ...


class DiscoverScreensProto(Protocol):
    """Free-function form of BFS screen discovery."""

    async def __call__(
        self,
        client: AutomationClient,
        max_screens: int = ...,
        screenshot_dir: str | None = ...,
        on_screen_discovered: Callable[[DiscoveredScreen], None] | None = ...,
    ) -> tuple[list[DiscoveredScreen], list[NavigationEdge]]: ...


class AutomationClientFactoryProto(Protocol):
    """Builds an AutomationClient for a given backend/platform."""

    def __call__(
        self,
        *,
        backend: str,
        platform: str,
        appium_url: str,
        capabilities: dict,
    ) -> AutomationClient: ...


def _default_app_acquirer_factory() -> AppAcquirerProto:
    # No ``minio_storage`` is passed — this is the ONLY construction site in
    # ``src/``, so minio:// app_path acquisition is inert in production: it
    # raises AppAcquisitionError("MinIO storage is not configured…"). The
    # bucket/tenant confinement in ``app_acquisition.confine_minio_uri`` is
    # therefore pre-positioned defence-in-depth, not a live-leak fix.
    return AppAcquisitionService()


def _default_automation_client_factory(
    *,
    backend: str,
    platform: str,
    appium_url: str,
    capabilities: dict,
) -> AutomationClient:
    if backend == "stub":
        # Offline deterministic 2-screen backend; bypasses Appium + device
        # farm entirely. Used by the orchestrator's app_review E2E scenario
        # so the inner pipeline (screen discovery + accessibility +
        # purpose inference + persistence) is exercised against synthetic
        # data without needing an APK/IPA or BrowserStack credentials.
        from src.services.stub_automation_client import StubAutomationClient

        return StubAutomationClient(platform=platform)
    if backend == "maestro":
        from src.services.maestro_client import MaestroClient

        return MaestroClient(platform=platform)
    return AppiumClient(
        appium_url=appium_url,
        capabilities=capabilities,
        platform=platform,
    )


class InspectorService:
    """Orchestrates mobile app inspection across device farms.

    Coordinates app acquisition, device provisioning, BFS screen discovery,
    accessibility analysis, and result persistence.

    All collaborators are injectable via constructor (Protocol-typed) for
    testing. Production callers pass nothing -- defaults wire the real
    implementations.
    """

    def __init__(
        self,
        storage: StorageService,
        *,
        job_manager: JobManagerProto | None = None,
        app_acquirer_factory: Callable[[], AppAcquirerProto] | None = None,
        device_farm_factory: DeviceFarmFactoryProto | None = None,
        discover_screens_fn: DiscoverScreensProto | None = None,
        automation_client_factory: AutomationClientFactoryProto | None = None,
    ) -> None:
        self._storage = storage
        self._job_manager: JobManagerProto = job_manager or cast(JobManagerProto, JobManager)
        self._app_acquirer_factory: Callable[[], AppAcquirerProto] = (
            app_acquirer_factory or _default_app_acquirer_factory
        )
        self._device_farm_factory: DeviceFarmFactoryProto = device_farm_factory or get_device_farm
        self._discover_screens: DiscoverScreensProto = discover_screens_fn or _real_discover_screens
        self._automation_client_factory: AutomationClientFactoryProto = (
            automation_client_factory or _default_automation_client_factory
        )

    async def create_job(
        self,
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
    ) -> str:
        """Create a new inspection job and return its ID."""
        if platform is None:
            platform = _detect_platform(app_path)
            logger.debug("Auto-detected platform=%s for %s", platform, app_path)

        job = await self._job_manager.create_job(
            db,
            tenant_id=tenant_id,
            app_path=app_path,
            platform=platform,
            automation_backend=automation_backend,
            device_farm_provider=device_farm_provider,
            max_screens=max_screens,
            enable_recording=enable_recording,
            require_selection=require_selection,
        )
        logger.info("Created inspection job %s for tenant=%s", job.job_id, tenant_id)
        return job.job_id

    async def _acquire_app(self, job: InspectionJob, job_id: str) -> tuple[str, bool]:
        """Acquire the app binary; return (local_path, is_temp).

        ``is_temp`` is True only when acquisition produced a tempfile it OWNS
        (URL / MinIO / play-store downloads) and the caller may safely delete
        it after use. It is False for a user-supplied LOCAL path, where
        ``local_path`` is the ORIGINAL source file — deleting it would be data
        loss. Updates job.platform if learned.
        """
        # Stub backend skips the whole APK/IPA path — there is no app to
        # acquire. Returning a non-None synthetic path keeps the downstream
        # caller (LocalFarm.upload_app, which is a passthrough) happy
        # without touching the filesystem or network. The actual content
        # is never read by StubAutomationClient.connect(). is_temp=True keeps
        # the prior finally behaviour (attempt unlink; missing_ok no-op on the
        # non-existent /dev/null path).
        if (job.automation_backend or "appium") == "stub":
            job.platform = job.platform or "android"
            return "/dev/null/stub.apk", True

        acquisition = self._app_acquirer_factory()
        t0 = time.monotonic()
        if job.app_path is None:
            raise RuntimeError(f"Job {job_id} has no app_path set")
        # The job's OWN tenant confines a minio:// app_path to that tenant's key
        # prefix (the route already rejected escapes; this keeps the guard live
        # for re-processed / directly-created jobs).
        acquired = await acquisition.acquire(job.app_path, tenant_id=job.tenant_id)
        logger.info(
            "App acquired in %.1fs: %s -> %s",
            time.monotonic() - t0,
            job.app_path,
            acquired.local_path,
        )
        if (job.platform is None or job.platform == "android") and acquired.platform:
            job.platform = acquired.platform
        return str(acquired.local_path), acquired.is_temp

    async def _connect_automation(
        self,
        job: InspectionJob,
        app_url: str,
        appium_url: str,
        extra_caps: dict,
        job_id: str,
    ) -> AutomationClient:
        """Construct and connect the automation client."""
        t0 = time.monotonic()
        platform = job.platform or "android"
        backend = job.automation_backend or "appium"
        client = self._automation_client_factory(
            backend=backend,
            platform=platform,
            appium_url=appium_url,
            capabilities=extra_caps,
        )
        await client.connect(app_url)
        logger.info(
            "Automation client (%s) connected in %.1fs for job %s",
            backend,
            time.monotonic() - t0,
            job_id,
        )
        return client

    async def _stop_recording(
        self, client: AutomationClient, job_id: str, enabled: bool
    ) -> str | None:
        if not enabled:
            return None
        try:
            video_data = await client.stop_recording()
        except OSError, RuntimeError, WebDriverException:
            logger.warning("Failed to stop/save recording for job %s", job_id)
            return None
        if not video_data:
            return None
        # Capture to the pod-local scratch dir, then STREAM to shared storage so
        # the recording resolves from any replica (the download endpoint may land
        # on a different pod). Writing to disk first lets ``store_recording`` use
        # a chunked ``fput_object`` instead of buffering the whole video through
        # an in-memory put.
        scratch = self._storage.get_job_dir(job_id) / "recording.mp4"
        await asyncio.to_thread(scratch.write_bytes, video_data)
        del video_data
        return await self._storage.store_recording(job_id, scratch)

    async def _persist_screenshots(self, job_id: str, screens_data: list[dict]) -> None:
        """Upload pod-local screenshots to shared storage and rewrite their refs.

        Screenshots are captured to the pod-local scratch dir during discovery;
        under the MinIO backend they must be uploaded so ``screenshot_path`` is a
        stable object key that resolves across replicas rather than a pod-local
        path that 404s on any other pod. No-op for the local backend (the scratch
        file already lives on the mounted volume).
        """
        if self._storage.backend != "minio":
            return
        for screen in screens_data:
            raw = screen.get("screenshot_path")
            if not raw:
                continue
            path = Path(raw)
            if not await asyncio.to_thread(path.is_file):
                # The scratch file is gone, so the pod-local path cannot be
                # uploaded and would 404 on every other replica. Drop it and
                # say so rather than persisting a path to nothing.
                logger.warning(
                    "Screenshot for job %s vanished before upload: %s", job_id, path.name
                )
                screen["screenshot_path"] = None
                screen["screenshot_status"] = "failed"
                continue
            data = await asyncio.to_thread(path.read_bytes)
            key = await self._storage.store_bytes(job_id, data, f"screenshots/{path.name}")
            screen["screenshot_path"] = key
            with contextlib.suppress(OSError):
                await asyncio.to_thread(path.unlink)

    async def _persist_results(
        self,
        db: AsyncSession,
        job: InspectionJob,
        job_id: str,
        tenant_id: str,
        screens: list[DiscoveredScreen],
        screens_data: list[dict],
        nav_graph: list[dict],
        elapsed: float,
        video_path: str | None,
    ) -> None:
        from src.services.purpose_inferrer import infer_purpose_heuristic

        estimated_purpose = infer_purpose_heuristic(screens_data, nav_graph)
        logger.info("Purpose inferred for job %s: %s", job_id, estimated_purpose[:120])

        common_kwargs: dict = dict(
            screens_discovered=len(screens),
            screens_data=screens_data,
            navigation_graph=nav_graph,
            inspection_time_seconds=elapsed,
            video_path=video_path,
            estimated_purpose=estimated_purpose,
        )
        if not job.require_selection:
            await self._job_manager.mark_completed(db, job_id, **common_kwargs)
        else:
            await self._job_manager.mark_awaiting_selection(db, job_id, **common_kwargs)

        captured = sum(1 for s in screens_data if s.get("screenshot_path"))
        logger.info(
            "Job %s completed: %d screens (%d with a screenshot) in %.1fs",
            job_id,
            len(screens),
            captured,
            elapsed,
        )
        if not screens:
            # The emptiest possible result, and the one the old `if screens and
            # not captured` guard was silent about: automation connected, the
            # hierarchy was blank/splash, find_clickable_elements returned
            # nothing, and the crawl ended with zero screens. "Found nothing"
            # and "never ran" are indistinguishable from the payload alone, so
            # say so LOUDLY here and name it on the wire as
            # InspectionResultResponse.degraded_reason='no_screens_discovered'.
            logger.warning(
                "Job %s completed with ZERO screens — nothing was inspected; "
                "treat this result as UNMEASURED, not as an empty app",
                job_id,
            )
        elif not captured:
            # Shape-complete, artifact-empty: the exact condition that let an
            # all-text-card app_review video ship on a clean green run. Surfaced
            # on the wire as InspectionResultResponse.artifacts_degraded.
            logger.warning(
                "Job %s completed with %d screens and ZERO screenshots", job_id, len(screens)
            )
        nav_failures = sum(int(s.get("navigation_failures") or 0) for s in screens_data)
        if nav_failures:
            # Dropped tap/back attempts. Surfaced on the wire as
            # InspectionResultResponse.navigation_failures so a consumer can
            # tell an incomplete navigation graph from a genuinely linear one.
            logger.warning(
                "Job %s dropped %d navigation attempt(s) across %d screens — "
                "the navigation graph (%d edges) is INCOMPLETE",
                job_id,
                nav_failures,
                len(screens),
                len(nav_graph),
            )
        # ``screens_discovered`` is DELIBERATELY not gated on artifact durability.
        # The metered unit is inspection work performed -- page-source fetch, XML
        # parse, classification, accessibility analysis, UI-component detection,
        # purpose inference -- none of which depends on a screenshot, and all of
        # which is delivered on the /result payload for a screenshot-less screen.
        # Gating it would also make the billed count diverge from the count this
        # same call reports, creating a second silent inconsistency. The genuine
        # defect was that a short-on-artifacts result was INVISIBLE; that is
        # fixed by artifacts_degraded/screenshots_captured above, not by
        # under-reporting real work.
        await self._job_manager.record_usage(
            db,
            tenant_id=tenant_id,
            inspections_completed=1,
            screens_discovered=len(screens),
        )

    async def _handle_failure(
        self,
        db: AsyncSession,
        job_id: str,
        tenant_id: str,
        exc: Exception,
        screens: list[DiscoveredScreen],
        screens_data: list[dict],
        *,
        farm_credentials: DeviceFarmCredentials | None = None,
    ) -> None:
        # No exc_info: the traceback repeats the unredacted exception message, and a
        # device-farm error can carry a tenant credential -- in a hub URL
        # (user:key@...), as a Basic header, or as a bare key in prose. The
        # known credentials of THIS job are scrubbed by exact value.
        safe_error = redact_farm_error(str(exc), farm_credentials)
        logger.error("Inspection job %s failed: %s: %s", job_id, type(exc).__name__, safe_error)
        if screens_data:
            partial_screens: list[dict] = screens_data
        elif screens:
            partial_screens = [
                {"screen_id": s.screen_id, "screen_type": s.screen_type} for s in screens
            ]
        else:
            partial_screens = []
        try:
            await self._job_manager.mark_failed_with_partial(
                db,
                job_id,
                # Last line of defence: app_path is caller-supplied and may
                # carry a credential. jobs.error_message is returned verbatim
                # by GET /api/inspect/{id}, so nothing unredacted may land here
                # even if a new raise site forgets to scrub its own message.
                safe_error,
                partial_screens_data=partial_screens if partial_screens else None,
            )
        except JobStateConflictError:
            logger.info(
                "Inspection job %s already terminal — skip mark_failed_with_partial",
                job_id,
            )
        await self._job_manager.record_usage(db, tenant_id=tenant_id, is_error=True)

    async def process_job(
        self,
        db: AsyncSession,
        job_id: str,
        tenant_id: str,
        *,
        farm_credentials: DeviceFarmCredentials | None = None,
    ) -> None:
        """Process a pending inspection job.

        ``farm_credentials`` are the per-request device farm credentials, held
        in memory only (never on the job row). A credentialed provider without
        them fails the job with a "resubmit" error instead of a TypeError.
        """
        job = await self._job_manager.get_job(db, job_id, tenant_id=tenant_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        logger.info("Processing inspection job %s (tenant=%s)", job_id, tenant_id)
        await self._job_manager.mark_processing(db, job_id)
        await db.commit()

        start = time.monotonic()
        client: AutomationClient | None = None
        screens: list[DiscoveredScreen] = []
        screens_data: list[dict] = []
        app_local_path: str | None = None
        app_is_temp = False

        try:
            provider = job.device_farm_provider or "local"
            farm = self._device_farm_factory(
                provider, **device_farm_kwargs(provider, farm_credentials)
            )
            logger.info("Using device farm provider=%s for job %s", farm.name, job_id)

            app_local_path, app_is_temp = await self._acquire_app(job, job_id)

            t0 = time.monotonic()
            app_url = await farm.upload_app(app_local_path)
            logger.info("App uploaded to farm in %.1fs", time.monotonic() - t0)
            appium_url = await farm.get_appium_url()
            extra_caps = await farm.get_capabilities(DeviceSpec(platform=job.platform or "android"))

            screenshot_dir = str(self._storage.get_screenshots_dir(job_id))
            client = await self._connect_automation(job, app_url, appium_url, extra_caps, job_id)

            if job.enable_recording:
                try:
                    await client.start_recording()
                except OSError, RuntimeError, WebDriverException:
                    logger.warning("Failed to start recording for job %s", job_id)

            t0 = time.monotonic()
            screens, edges = await self._discover_screens(
                client,
                max_screens=job.max_screens or DEFAULT_MAX_SCREENS,
                screenshot_dir=screenshot_dir,
            )
            logger.info(
                "Screen discovery completed for job %s: %d screens, %d edges in %.1fs",
                job_id,
                len(screens),
                len(edges),
                time.monotonic() - t0,
            )
            elapsed = time.monotonic() - start

            video_path = await self._stop_recording(client, job_id, bool(job.enable_recording))

            screens_data = [_serialize_screen(s) for s in screens]
            await self._persist_screenshots(job_id, screens_data)
            nav_graph = [_serialize_edge(e) for e in edges]

            await self._persist_results(
                db, job, job_id, tenant_id, screens, screens_data, nav_graph, elapsed, video_path
            )

        except (
            OSError,
            RuntimeError,
            WebDriverException,
            AppAcquisitionError,
            SSRFError,
            httpx.HTTPError,
        ) as exc:
            # AppAcquisitionError covers DownloadError + ValidationError (its
            # subclasses) from _acquire_app; SSRFError is a ValueError raised by
            # the URL guard; httpx.HTTPError covers HTTPStatusError from
            # farm.upload_app. All must route to _handle_failure so per-tenant
            # error usage is recorded, not silently skipped. When handled here the
            # exception does NOT propagate, so the outer _process_inspection_bg
            # wrapper's mark_failed does not double-fail the job.
            await self._handle_failure(
                db,
                job_id,
                tenant_id,
                exc,
                screens,
                screens_data,
                farm_credentials=farm_credentials,
            )

        finally:
            if client:
                try:
                    await client.disconnect()
                except OSError, RuntimeError, WebDriverException:
                    logger.warning("Failed to disconnect automation client")
            # Delete the downloaded app artifact (tempfile.mkstemp output from
            # app_acquisition, ≤500MB) after the farm upload has consumed it, on
            # BOTH success and failure — otherwise every successful inspection
            # leaks a full APK until the disk fills and mkstemp/write raise
            # OSError (self-DoS). The stub backend's synthetic /dev/null path and
            # the failure-path unlinks in app_acquisition are both covered by
            # missing_ok=True (no double-unlink error).
            #
            # ONLY unlink when acquisition owns the file (app_is_temp). A LOCAL
            # -path input resolves to the USER'S ORIGINAL file (acquisition
            # `_from_local_path` returns it unchanged, is_temp=False, and local
            # paths are enabled by default) — deleting it is DATA LOSS and breaks
            # retry/resume + concurrent jobs sharing the input.
            if app_local_path and app_is_temp:
                with contextlib.suppress(OSError):
                    Path(app_local_path).unlink(missing_ok=True)

        await db.commit()


def _serialize_screen(s: DiscoveredScreen) -> dict:
    return {
        "screen_id": s.screen_id,
        "screen_type": s.screen_type,
        "activity_name": s.activity_name,
        "text_content": s.text_content,
        "interactive_elements": s.interactive_element_count,
        "accessibility_ids": s.accessibility_ids,
        "screenshot_path": s.screenshot_path,
        "screenshot_status": s.screenshot_status,
        "headings": s.headings,
        "has_forms": s.has_forms,
        "has_media": s.has_media,
        "content_sections": s.content_sections,
        "accessibility_issues": s.accessibility_issues,
        "accessibility_score": s.accessibility_score,
        "interactive_elements_detail": s.interactive_elements_detail,
        "ui_components": s.ui_components,
        "navigation_failures": s.navigation_failures,
    }


def _serialize_edge(e: NavigationEdge) -> dict:
    return {
        "from_screen": e.from_screen,
        "to_screen": e.to_screen,
        "action": e.action,
        "element_label": e.element_label,
    }


def _detect_platform(app_path: str) -> str:
    """Detect platform from app file extension."""
    lower = app_path.lower()
    if lower.endswith((".ipa", ".app")):
        return "ios"
    return "android"
