"""Comparison endpoint for diffing two inspection jobs."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import STATUS_COMPLETED
from src.api.routes._helpers import _build_screen_info, _get_tenant_id
from src.api.schemas import (
    CompareRequest,
    InspectionDiffResponse,
    NavigationChangeInfo,
    ScreenChangeInfo,
)
from src.database.session import get_db
from src.services.job_manager import JobManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inspect"])


@router.post(
    "/compare",
    operation_id="compare_app_inspections",
    summary="Compare two completed inspections for diffing screens + navigation",
    responses={
        404: {"description": "Baseline or comparison job not found"},
        409: {"description": "Baseline or comparison job not completed yet"},
        422: {"description": "Request body validation error"},
    },
)
async def compare_inspections_endpoint(
    body: CompareRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> InspectionDiffResponse:
    """Compare two completed inspections to detect screen changes.

    Args:
        body: Contains the two job IDs to compare.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        InspectionDiffResponse with added, removed, and changed screens.

    Raises:
        HTTPException: If either job is not found or not completed.
    """
    logger.info(
        "Compare requested: baseline=%s comparison=%s",
        body.baseline_job_id,
        body.comparison_job_id,
    )
    t0 = time.monotonic()
    tenant_id = _get_tenant_id(request)
    job_before = await JobManager.get_job(db, body.baseline_job_id, tenant_id=tenant_id)
    if job_before is None:
        raise HTTPException(
            status_code=404,
            detail=f"Baseline job not found: {body.baseline_job_id}",
        )
    if job_before.status != STATUS_COMPLETED:
        raise HTTPException(
            status_code=409,
            detail=f"Baseline job not completed (status: {job_before.status})",
            headers={"Retry-After": "30"},
        )

    job_after = await JobManager.get_job(db, body.comparison_job_id, tenant_id=tenant_id)
    if job_after is None:
        raise HTTPException(
            status_code=404,
            detail=f"Comparison job not found: {body.comparison_job_id}",
        )
    if job_after.status != STATUS_COMPLETED:
        raise HTTPException(
            status_code=409,
            detail=f"Comparison job not completed (status: {job_after.status})",
            headers={"Retry-After": "30"},
        )

    from src.services.inspection_diff import compare_inspections

    result = compare_inspections(
        screens_before=job_before.screens_data or [],
        screens_after=job_after.screens_data or [],
        nav_before=job_before.navigation_graph or [],
        nav_after=job_after.navigation_graph or [],
    )

    response = InspectionDiffResponse(
        added_screens=[_build_screen_info(s) for s in result.added_screens],
        removed_screens=[_build_screen_info(s) for s in result.removed_screens],
        changed_screens=[
            ScreenChangeInfo(
                screen_id_before=c.screen_id_before,
                screen_id_after=c.screen_id_after,
                screen_type_before=c.screen_type_before,
                screen_type_after=c.screen_type_after,
                activity_name=c.activity_name,
                text_similarity=c.text_similarity,
                interactive_elements_before=c.interactive_elements_before,
                interactive_elements_after=c.interactive_elements_after,
                change_summary=c.change_summary,
            )
            for c in result.changed_screens
        ],
        unchanged_screen_ids=result.unchanged_screen_ids,
        navigation_changes=[
            NavigationChangeInfo(
                from_screen=n.from_screen,
                to_screen=n.to_screen,
                action=n.action,
                element_label=n.element_label,
                change_type=n.change_type,
            )
            for n in result.navigation_changes
        ],
        summary=result.summary,
    )
    logger.info(
        "Comparison completed in %.3fs: %s",
        time.monotonic() - t0,
        result.summary,
    )
    return response
