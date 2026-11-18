"""Screen selection endpoints (preview and select)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import STATUS_AWAITING_SELECTION, STATUS_COMPLETED
from src.api.routes._helpers import _get_tenant_id
from src.api.schemas import (
    ScreenPreview,
    ScreenSelectionRequest,
    ScreenSelectionResponse,
)
from src.database.session import get_db
from src.services.job_manager import JobManager, JobStateConflictError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inspect"])


@router.get(
    "/{job_id}/screens/preview",
    operation_id="get_inspection_screens_preview",
    summary="Get screen previews for selection",
    responses={
        404: {"description": "Job not found"},
    },
)
async def get_screens_preview(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[ScreenPreview]:
    """Get screen previews for selection.

    Args:
        job_id: Unique job identifier.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        List of ScreenPreview objects.

    Raises:
        HTTPException: If the job is not found.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Screen preview requested for job_id=%s", job_id)
    job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        logger.warning("Job not found: %s", job_id)
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    selected_ids = set(job.selected_screen_ids or [])
    previews = []
    for s in job.screens_data or []:
        sid = s.get("screen_id", "")
        previews.append(
            ScreenPreview(
                screen_id=sid,
                screen_type=s.get("screen_type", "unknown"),
                activity_name=s.get("activity_name"),
                text_preview=(s.get("text_content") or "")[:200] or None,
                interactive_elements=s.get("interactive_elements", 0),
                screenshot_path=s.get("screenshot_path"),
                selected=sid in selected_ids if selected_ids else True,
            )
        )
    logger.info("Returning %d screen previews for job %s", len(previews), job_id)
    return previews


@router.post(
    "/{job_id}/screens/select",
    operation_id="select_inspection_screens",
    summary="Submit screen selection for an inspection job",
    responses={
        400: {"description": "Invalid screen IDs"},
        404: {"description": "Job not found"},
        409: {"description": "Job not in selectable state"},
        422: {"description": "Request body validation error"},
    },
)
async def select_screens(
    job_id: str,
    body: ScreenSelectionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScreenSelectionResponse:
    """Submit screen selection for a job.

    Args:
        job_id: Unique job identifier.
        body: Contains the list of selected screen IDs.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        ScreenSelectionResponse with status and selected count.

    Raises:
        HTTPException: If the job is not found, not selectable, or IDs are invalid.
    """
    tenant_id = _get_tenant_id(request)
    logger.info(
        "Screen selection for job_id=%s: %d screens selected",
        job_id,
        len(body.selected_screen_ids),
    )
    job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        logger.warning("Job not found: %s", job_id)
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if job.status not in (STATUS_COMPLETED, STATUS_AWAITING_SELECTION):
        logger.warning("Job %s not selectable (status=%s)", job_id, job.status)
        raise HTTPException(
            status_code=409,
            detail=f"Job not in selectable state (status: {job.status})",
        )

    # Validate screen IDs
    valid_ids = {s.get("screen_id", "") for s in (job.screens_data or [])}
    invalid = set(body.selected_screen_ids) - valid_ids
    if invalid:
        logger.warning("Invalid screen IDs for job %s: %s", job_id, sorted(invalid))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid screen IDs: {sorted(invalid)}",
        )

    try:
        await JobManager.update_screen_selection(
            db, job_id, body.selected_screen_ids, tenant_id=tenant_id
        )
    except JobStateConflictError:
        logger.warning("Job %s lost the selection write race (status changed)", job_id)
        raise HTTPException(
            status_code=409,
            detail=f"Job not in selectable state (status: {job.status})",
        ) from None
    await db.commit()
    logger.info(
        "Screen selection saved for job %s: %d screens",
        job_id,
        len(body.selected_screen_ids),
    )
    return ScreenSelectionResponse(
        status=STATUS_COMPLETED, selected_count=len(body.selected_screen_ids)
    )
