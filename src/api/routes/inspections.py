"""Inspection creation endpoint and background processing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from w2v_common.auth.identity_context import (
    get_tenant_source_from_context,
    set_identity_context,
)

from src.api.constants import STATUS_PENDING
from src.api.routes._helpers import _get_inspector_service, _get_tenant_id
from src.api.schemas import AsyncJobResponse, InspectRequest
from src.config import get_settings
from src.database.session import get_db, get_db_session
from src.services.app_acquisition import (
    SUPPORTED_URI_SCHEMES,
    URI_SCHEME_RE,
    ConfinementError,
    confine_minio_uri,
    resolve_allowed_minio_bucket,
)
from src.services.device_farm import DeviceFarmCredentials, redact_farm_error
from src.services.inspector_service import InspectorService
from src.services.job_manager import JobManager, JobStateConflictError
from src.tenant import require_tenant
from src.utils.redaction import redact_url_credentials

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks

router = APIRouter(prefix="/api/inspect", tags=["inspect"])


def _resolve_automation_backend(request_backend: str) -> str:
    """Honor ``APP_INSPECTOR_DEFAULT_BACKEND`` env when no explicit backend.

    Pydantic surfaces the schema default ``"appium"`` when the client
    omitted the field, indistinguishable from an explicit ``"appium"``
    on the wire. Production deployments leave the env unset and
    ``"appium"`` is honored verbatim. Pre-prod / E2E deployments set
    ``APP_INSPECTOR_DEFAULT_BACKEND=stub`` to drive the orchestrator's
    ``app_inspect`` stage through the offline stub backend
    (StubAutomationClient) without changing the orchestrator's client.

    Explicitly-set ``"maestro"`` and ``"stub"`` always pass through —
    the env only overrides the implicit/default ``"appium"`` so an
    operator can pin a backend without forcing callers to opt in.
    """
    env_default = os.getenv("APP_INSPECTOR_DEFAULT_BACKEND")
    if env_default and request_backend == "appium":
        return env_default
    return request_backend


def _validate_app_path(app_path: str, *, tenant_id: str) -> str:
    """Validate app_path: SSRF-bound URLs, confined MinIO refs, confined local paths.

    - ``http(s)://`` URLs (incl. Play Store links) pass through here and are
      SSRF-validated downstream by the acquisition service.
    - ``minio://`` refs are confined HERE to the single configured bucket AND
      to the caller's own ``<tenant_id>/`` key prefix — ``w2v_common``'s
      StorageService has no tenant concept, so any object in any readable
      bucket would otherwise be nameable. The normalised URI is returned so the
      value persisted on the job is the one that was validated; the object key
      keeps its RAW (percent-encoded) form, because ``%`` is legal in a MinIO
      key and decoding it would name a different object (see
      ``confine_minio_uri``).

      NOTE — this is defence-in-depth on an input scheme that does not
      currently work at all: nothing in ``src/`` passes ``minio_storage`` to
      ``AppAcquisitionService``, so a ``minio://`` app_path already failed in
      the background job with "MinIO storage is not configured". The confinement
      rule also has an EMPTY accept-set today (no producer writes under
      ``<tenant>/`` in the allowed bucket) — both caveats are documented at
      length in ``src/services/app_acquisition.py`` § Storage confinement.

    The one behaviour change this validation ships: a bad ``app_path`` is
    rejected with 400 at submit time instead of being accepted (202) and
    failing later inside the job.
    - Any other scheme (``s3://``, ``file://``, …) is rejected outright rather
      than falling through to the local-path branch.
    - Local file paths are normalized and checked against the configured
      ``allowed_local_paths``.

    Returns:
        The validated (and possibly normalized) app_path.

    Raises:
        HTTPException 400: On traversal components, a foreign bucket, a key
            outside the tenant prefix, an unsupported scheme, or a path outside
            the allowed directories.
    """
    stripped = app_path.strip()

    # HTTP(S) inputs are SSRF-validated downstream by the acquisition service.
    if stripped.startswith(("http://", "https://")):
        return stripped

    if stripped.startswith("minio://"):
        try:
            bucket, key = confine_minio_uri(
                stripped,
                tenant_id=tenant_id,
                allowed_bucket=resolve_allowed_minio_bucket(get_settings()),
            )
        except ConfinementError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return f"minio://{bucket}/{key}"

    if URI_SCHEME_RE.match(stripped):
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported URI scheme in app_path. Supported: "
                f"{', '.join(SUPPORTED_URI_SCHEMES)} or an allowed local path."
            ),
        )

    # Reject paths that contain explicit traversal components *before*
    # resolution so that the intent is clear regardless of symlinks.
    if ".." in Path(stripped).parts:
        raise HTTPException(
            status_code=400,
            detail="Path traversal components ('..') are not allowed in app_path.",
        )

    resolved = Path(stripped).resolve()
    settings = get_settings()
    allowed_bases = [Path(p).resolve() for p in settings.acquisition.allowed_local_paths]

    if not any(resolved.is_relative_to(base) for base in allowed_bases):
        allowed_dirs = ", ".join(str(b) for b in allowed_bases)
        raise HTTPException(
            status_code=400,
            detail=(
                f"app_path must reside within an allowed directory ({allowed_dirs}). "
                f"Resolved path '{resolved}' is outside all allowed directories."
            ),
        )

    return stripped


@router.post(
    "",
    operation_id="start_app_inspection",
    summary="Start a mobile app inspection job",
    responses={
        400: {
            "description": (
                "Invalid app_path (path traversal, unsupported scheme, foreign MinIO "
                "bucket, or outside the tenant prefix / allowed directories)"
            )
        },
        422: {"description": "Request body validation error"},
    },
)
async def start_inspection(
    body: InspectRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AsyncJobResponse:
    """Start a mobile app inspection (async).

    Args:
        body: Inspection request parameters including app path and options.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        AsyncJobResponse with the job ID and poll URL.
    """
    tenant_id = _get_tenant_id(request)
    service = _get_inspector_service(request)

    # Validate app_path early to reject traversal / cross-tenant storage reads
    # before any DB writes.
    validated_app_path = _validate_app_path(body.app_path, tenant_id=tenant_id)

    logger.info(
        "Inspection requested by tenant=%s app_path=%s platform=%s",
        tenant_id,
        redact_url_credentials(validated_app_path),
        body.platform,
    )

    t0 = time.monotonic()
    job_id = await service.create_job(
        db,
        tenant_id=tenant_id,
        app_path=validated_app_path,
        platform=body.platform,
        automation_backend=_resolve_automation_backend(body.automation_backend),
        device_farm_provider=body.device_farm.provider,
        max_screens=body.max_screens,
        enable_recording=body.enable_recording,
        require_selection=body.require_selection,
    )
    await db.commit()
    logger.debug("Job %s created in %.3fs", job_id, time.monotonic() - t0)

    # Credentials are handed to the background task in memory ONLY -- the job
    # row carries just the provider name (see DeviceFarmConfig).
    farm = body.device_farm
    farm_credentials = (
        DeviceFarmCredentials(
            username=farm.browserstack_username,
            access_key=farm.browserstack_access_key,
        )
        if farm.browserstack_username is not None and farm.browserstack_access_key is not None
        else None
    )
    task = asyncio.create_task(
        _process_inspection_bg(service, job_id, tenant_id, farm_credentials=farm_credentials)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info("Inspection job %s accepted for tenant=%s", job_id, tenant_id)
    return AsyncJobResponse(
        job_id=job_id,
        status=STATUS_PENDING,
        poll_url=f"/api/inspect/{job_id}",
    )


async def _process_inspection_bg(
    service: InspectorService,
    job_id: str,
    tenant_id: str,
    *,
    farm_credentials: DeviceFarmCredentials | None = None,
) -> None:
    """Run the inspection job in a background task.

    On failure, marks the job as failed in a separate DB session.
    """
    # This task runs in its own asyncio context (asyncio.create_task copies the
    # request context, but make the tenant binding explicit + robust): seed the
    # identity ContextVar so w2v_common's session bind AND the after_begin RLS
    # listener apply app.current_tenant on every transaction of this job.
    #
    # w2v-common 0.67.0: the identity ContextVar is a real uuid.UUID and an
    # unparseable tenant RAISES instead of being substituted with the nil uuid.
    # require_tenant() is the single coercion point; a job that reached here
    # without a real tenant must die loudly rather than run un-attributed.
    #
    # ⚠️ tenant_source is LOAD-BEARING, not bookkeeping. The comment above is right
    # that create_task copies the request context — which is exactly why omitting
    # tenant_source here is destructive rather than merely incomplete:
    # set_identity_context does `_tenant_source_var.set(tenant_source)`
    # UNCONDITIONALLY, so the bare three-argument call OVERWRITES the provenance
    # AuthMiddleware already bound, with None. build_outbound_identity then reports
    # None as 'unrecorded':
    #   refusing to delegate tenant <t> to metered destination <d>:
    #   provenance is 'unrecorded', which is not vouched
    # Measured 2026-08-28: that is why modality-gateway refused panel image
    # generation in the w2v_frontend E2E matrix (same defect, ACG #109).
    # Reading it back and passing it through is a PRESERVE, not a re-derive.
    #
    # ⛔ Do NOT copy this into a reaper/worker loop that iterates rows for DIFFERENT
    # tenants: there the inherited value belongs to another tenant, and preserving
    # it would vouch for a tenant whose provenance was never established.
    set_identity_context(
        require_tenant(tenant_id, source="_process_inspection_bg"),
        None,
        None,
        tenant_source=get_tenant_source_from_context(),
    )
    try:
        async with get_db_session() as db:
            await service.process_job(db, job_id, tenant_id, farm_credentials=farm_credentials)
    except JobStateConflictError:
        logger.info(
            "Inspection job %s lost a state-transition race — another worker owns it",
            job_id,
        )
    except asyncio.CancelledError:
        # ⚠️ CancelledError is a BaseException — the ``except Exception`` below can
        # NEVER see it. Without this clause a cancelled task (rolling deploy /
        # SIGTERM: the loop closes and every task in ``_background_tasks`` is
        # cancelled) leaves the row in whatever non-terminal state it reached, and
        # a client polling GET /api/inspect/{job_id} watches it forever.
        #
        # The DB sweep (``prune_stuck_inspection_jobs``, widened to ``pending`` in
        # migration 0008) is the durable backstop — it reclaims the row after
        # ``CLEANUP_STUCK_JOB_TIMEOUT_MINUTES``. This clause is the fast path that
        # makes the poller see the truth immediately instead of up to an hour late.
        # Best-effort by construction: during interpreter/loop teardown the write
        # itself may not complete, hence the suppress — the sweep still covers it.
        logger.warning("Inspection job %s cancelled mid-flight — marking failed", job_id)
        with contextlib.suppress(Exception):
            async with get_db_session() as db:
                await JobManager.mark_failed(db, job_id, "Job cancelled (worker shutdown)")
        raise
    except Exception as exc:  # noqa: BLE001 -- logged (redacted) + job marked failed
        # The error text can quote the tenant's device-farm access key (bare, in
        # a hub URL, or as a Basic header). Scrub with the KNOWN credentials of
        # this job before it is logged or persisted: jobs.error_message is
        # returned by GET /api/inspect/{id} and logged raw by the orchestrator's
        # poll_job. No exc_info -- the traceback repeats the unredacted message.
        safe_error = redact_farm_error(str(exc), farm_credentials)
        logger.error(
            "Background inspection job %s failed: %s: %s", job_id, type(exc).__name__, safe_error
        )
        # Mark the job as failed in a separate session to avoid rollback
        try:
            async with get_db_session() as db:
                await JobManager.mark_failed(db, job_id, safe_error)
        except JobStateConflictError:
            logger.info("Inspection job %s already in terminal state — skip mark_failed", job_id)
        except Exception:
            logger.exception("Failed to mark job %s as failed", job_id)
