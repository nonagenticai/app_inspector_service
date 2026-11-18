"""Export endpoint for HTML and XML inspection reports."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import (
    EXPORT_HTML,
    EXPORT_XML,
    STATUS_AWAITING_SELECTION,
    STATUS_COMPLETED,
)
from src.api.routes._helpers import _get_tenant_id
from src.database.session import get_db
from src.services.job_manager import JobManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inspect"])


@router.get(
    "/{job_id}/export/{format}",
    operation_id="export_inspection_report",
    summary="Export inspection results as HTML or XML",
    responses={
        400: {"description": "Unsupported export format"},
        404: {"description": "Job not found"},
        409: {"description": "Job not completed yet"},
    },
)
async def export_inspection(
    request: Request,
    job_id: str,
    fmt: str = Path(alias="format"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export inspection results as HTML or XML.

    Args:
        job_id: Unique job identifier.
        fmt: Export format, either ``html`` or ``xml``.
        request: The incoming HTTP request.
        db: Async database session (injected).

    Returns:
        Response with the exported content.

    Raises:
        HTTPException: If the job is not found, not completed, or format is invalid.
    """
    from src.services.export_service import HTML_REPORT_CSP, ExportService

    tenant_id = _get_tenant_id(request)
    logger.info("Export requested for job_id=%s format=%s", job_id, fmt)
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

    screens_data = job.screens_data or []
    # Filter by selection if applicable
    if job.selected_screen_ids is not None:
        selected_set = set(job.selected_screen_ids)
        screens_data = [s for s in screens_data if s.get("screen_id") in selected_set]

    export_svc = ExportService()

    if fmt == EXPORT_HTML:
        content = export_svc.export_html(
            job_id=job.job_id,
            platform=job.platform,
            screens=screens_data,
            navigation_graph=job.navigation_graph or [],
            estimated_purpose=job.estimated_purpose,
        )
        logger.info("Exported HTML report for job %s (%d screens)", job_id, len(screens_data))
        # The report is rendered from the inspected app's OWN UI text and is
        # served as text/html from this service's origin. Escaping in
        # ExportService is the fix; the CSP is the second wall so that a single
        # future missed interpolation is not same-origin script execution.
        # nosniff stops a UA from re-typing the body into something scriptable.
        return Response(
            content=content,
            media_type="text/html",
            headers={
                "Content-Security-Policy": HTML_REPORT_CSP,
                "X-Content-Type-Options": "nosniff",
            },
        )
    if fmt == EXPORT_XML:
        content = export_svc.export_xml_sitemap(
            job_id=job.job_id,
            platform=job.platform,
            screens=screens_data,
            navigation_graph=job.navigation_graph or [],
        )
        logger.info("Exported XML sitemap for job %s (%d screens)", job_id, len(screens_data))
        return Response(content=content, media_type="application/xml")
    logger.warning("Unsupported export format: %s", fmt)
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported export format: {fmt}. Use 'html' or 'xml'.",
    )
