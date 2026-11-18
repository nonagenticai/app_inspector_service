"""Job status, result, screens, and video endpoints."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import STATUS_AWAITING_SELECTION, STATUS_COMPLETED
from src.api.routes._helpers import _build_screen_info, _get_tenant_id
from src.api.schemas import (
    InspectionResultResponse,
    JobStatus,
    JobStatusResponse,
    NavigationEdge,
    ScreenInfo,
)
from src.cache.redis_client import get_redis_client
from src.database.session import get_db
from src.services.job_manager import JobManager
from src.services.storage_service import StorageService, get_storage_service
from src.utils.redaction import redact_error_message

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inspect"])

# Request headers forwarded to MinIO so ``<video>`` Range seeks / conditional
# fetches work when proxying the recording. Whitelist only.
_FORWARDED_RANGE_HEADERS: frozenset[str] = frozenset(
    {"range", "if-range", "if-modified-since", "if-none-match"}
)


async def _presign_screens_in_place(storage: StorageService, screens: list[ScreenInfo]) -> None:
    """Rewrite each screen's ``screenshot_path`` object key to a presigned URL.

    Screenshots are stored as MinIO object keys (stable across replicas); serve
    them as short-lived presigned GET URLs so a consumer can fetch them without
    a per-object proxy route. Best-effort — a presign failure leaves the raw ref
    (never fatal to returning the result).
    """
    if storage.backend != "minio":
        return
    for screen in screens:
        if not screen.screenshot_path:
            continue
        url = await storage.presign(screen.screenshot_path)
        if url:
            screen.screenshot_path = url


@router.get(
    "/{job_id}",
    operation_id="get_inspection_status",
    summary="Poll the status of an inspection job",
    responses={
        404: {"description": "Job not found"},
    },
)
async def get_job_status(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JobStatusResponse:
    """Poll the status of an inspection job.

    Args:
        job_id: Unique job identifier.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        JobStatusResponse with current status and progress info.

    Raises:
        HTTPException: If the job is not found.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Status poll for job_id=%s", job_id)
    # Check Redis cache first
    try:
        from src.cache.cache_keys import JOB_STATUS_TTL, job_status_key

        redis_client = get_redis_client()
        cached = await redis_client.get(job_status_key(tenant_id, job_id))
        if cached and isinstance(cached, dict):
            logger.debug("Cache hit for job_id=%s", job_id)
            return JobStatusResponse(**cached)
    except redis.RedisError, RuntimeError:  # Redis failures are non-fatal
        logger.warning("Redis cache lookup failed for job_id=%s, falling back to DB", job_id)

    logger.debug("Fetching job %s from database", job_id)
    job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        logger.warning("Job not found: %s", job_id)
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    result_url = f"/api/inspect/{job_id}/result" if job.status == STATUS_COMPLETED else None

    response = JobStatusResponse(
        job_id=job.job_id,
        status=cast(JobStatus, job.status),
        screens_discovered=job.screens_discovered or 0,
        created_at=job.created_at.isoformat() if job.created_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
        error=redact_error_message(job.error_message) if job.error_message else None,
        result_url=result_url,
    )

    # Cache the response
    try:
        from src.cache.cache_keys import JOB_STATUS_TTL, job_status_key

        redis_client = get_redis_client()
        await redis_client.set(
            job_status_key(tenant_id, job_id), response.model_dump(), ttl=JOB_STATUS_TTL
        )
    except (redis.RedisError, RuntimeError) as exc:  # Redis failures are non-fatal
        logger.warning("Failed to cache job status response: %s", exc)

    logger.info("Job %s status=%s screens=%d", job_id, job.status, job.screens_discovered or 0)
    return response


def _overall_accessibility_score(screens_raw: list[dict]) -> int | None:
    """Average the accessibility scores that were actually MEASURED.

    Returns None when nothing was measured — zero screens discovered, or every
    screen missing a score. The previous ``... if acc_scores else 100`` turned a
    total inspection failure into a perfect audit, and ``export_service`` then
    printed "Screens Discovered: 0 / Overall Accessibility Score: 100/100" into
    the customer's report.

    Screens without a score are EXCLUDED from the mean rather than counted as
    100, so a partially-analysed inspection cannot be inflated by the screens
    that failed to analyse.
    """
    measured = [
        score for s in screens_raw if isinstance(score := s.get("accessibility_score"), int)
    ]
    if not measured:
        return None
    return sum(measured) // len(measured)


@router.get(
    "/{job_id}/result",
    operation_id="get_inspection_result",
    summary="Retrieve the full inspection result for a completed job",
    responses={
        404: {"description": "Job not found"},
        409: {"description": "Job not completed yet"},
    },
)
async def get_inspection_result(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> InspectionResultResponse:
    """Retrieve the full inspection result for a completed job.

    Args:
        job_id: Unique job identifier.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        InspectionResultResponse with screens, navigation graph, and scores.

    Raises:
        HTTPException: If the job is not found or not yet completed.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Result requested for job_id=%s", job_id)
    job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        logger.warning("Job not found: %s", job_id)
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if job.status not in (STATUS_COMPLETED, STATUS_AWAITING_SELECTION):
        logger.warning("Job %s not completed (status=%s)", job_id, job.status)
        raise HTTPException(
            status_code=409,
            detail=f"Job not completed (status: {job.status})",
            headers={"Retry-After": "30"},
        )

    screens_raw = job.screens_data or []
    # Filter by selected screen IDs if set
    if job.selected_screen_ids is not None:
        selected_set = set(job.selected_screen_ids)
        screens_raw = [s for s in screens_raw if s.get("screen_id") in selected_set]

    screens = [_build_screen_info(s) for s in screens_raw]

    storage = get_storage_service(request)
    recording_ref = job.video_path
    if storage is not None and storage.backend == "minio":
        await _presign_screens_in_place(storage, screens)
        if recording_ref:
            presigned = await storage.presign(recording_ref)
            if presigned:
                recording_ref = presigned

    nav_edges = [
        NavigationEdge(
            from_screen=e.get("from_screen") or "",
            to_screen=e.get("to_screen") or "",
            action=e.get("action") or "tap",
            element_label=e.get("element_label"),
        )
        for e in (job.navigation_graph or [])
    ]

    overall_acc = _overall_accessibility_score(screens_raw)

    logger.info(
        "Returning result for job %s: %d screens, acc_score=%s",
        job_id,
        len(screens),
        overall_acc,
    )
    return InspectionResultResponse(
        job_id=job.job_id,
        status=cast(JobStatus, job.status),
        platform=job.platform,
        app_path=job.app_path,
        screens=screens,
        navigation_graph=nav_edges,
        screens_discovered=job.screens_discovered or 0,
        inspection_time_seconds=job.inspection_time_seconds,
        error=redact_error_message(job.error_message) if job.error_message else None,
        overall_accessibility_score=overall_acc,
        estimated_purpose=job.estimated_purpose,
        recording_path=recording_ref,
    )


@router.get(
    "/{job_id}/screens",
    operation_id="list_inspection_screens",
    summary="List all discovered screens for an inspection job",
    responses={
        404: {"description": "Job not found"},
    },
)
async def list_screens(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[ScreenInfo]:
    """List all discovered screens for an inspection job.

    Args:
        job_id: Unique job identifier.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        List of ScreenInfo objects.

    Raises:
        HTTPException: If the job is not found.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Listing screens for job_id=%s", job_id)
    job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        logger.warning("Job not found: %s", job_id)
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    screens = [_build_screen_info(s) for s in (job.screens_data or [])]
    logger.info("Returning %d screens for job %s", len(screens), job_id)
    return screens


@router.get(
    "/{job_id}/video",
    operation_id="download_inspection_recording",
    summary="Stream the recorded video for an inspection job",
    responses={
        404: {"description": "Job or recording not found"},
    },
)
async def get_job_video(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream the recorded video for an inspection job.

    Under the MinIO backend the recording is streamed straight from the shared
    bucket (constant memory, resolves regardless of which replica captured it or
    which replica serves the download). Under the local backend it is served
    from the pod-local scratch volume.

    Args:
        job_id: Unique job identifier.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        A streaming response carrying the MP4 recording.

    Raises:
        HTTPException: If the job, recording path, or object is not found.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Video requested for job_id=%s", job_id)
    job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        logger.warning("Job not found: %s", job_id)
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if not job.video_path:
        logger.warning("No recording available for job %s", job_id)
        raise HTTPException(status_code=404, detail="No recording available for this job")

    storage = get_storage_service(request)
    content_disposition = f'attachment; filename="inspection_{job_id}.mp4"'

    if storage is not None and storage.backend == "minio":
        range_headers = {
            k: v for k, v in request.headers.items() if k.lower() in _FORWARDED_RANGE_HEADERS
        }
        stream = await storage.open_recording_stream(job.video_path, request_headers=range_headers)
        if stream is None:
            logger.warning("Recording object %s not found for job %s", job.video_path, job_id)
            raise HTTPException(status_code=404, detail="Recording file not found")
        headers = dict(stream.headers)
        headers["content-disposition"] = content_disposition
        logger.info("Streaming recording for job %s from MinIO key %s", job_id, job.video_path)
        return StreamingResponse(
            stream.body(),
            status_code=stream.status,
            media_type=stream.headers.get("content-type", "video/mp4"),
            headers=headers,
        )

    # Local backend (dev/test) — serve from the mounted scratch volume.
    video_path = Path(job.video_path)
    if not video_path.exists():
        logger.warning("Recording file not found at %s for job %s", video_path, job_id)
        raise HTTPException(status_code=404, detail="Recording file not found")

    logger.info("Streaming video for job %s from %s", job_id, video_path)
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"inspection_{job_id}.mp4",
    )
