"""Shared helpers for route sub-modules."""

from __future__ import annotations

from fastapi import HTTPException, Request
from w2v_common.tenant import InvalidTenantIdError, MissingTenantError

from src.api.schemas import (
    AccessibilityIssueInfo,
    ContentSectionInfo,
    InteractiveElementInfo,
    ScreenInfo,
)
from src.services.inspector_service import InspectorService
from src.tenant import require_tenant


def _get_tenant_id(request: Request) -> str:
    """Return the caller's real tenant uuid in canonical dashed form, or 401.

    ``request.state.tenant_id`` is set by ``w2v_common.auth.middleware.AuthMiddleware``,
    which ``w2v_common.asgi.create_app`` registers for this service (``src/asgi.py``).
    It is populated from the license-token introspection payload or the JWT
    ``tenant_id`` claim.

    This used to return the literal ``"anonymous"`` when the request carried no
    identity. That sentinel is a data-scoping hole, not a default: every
    ``JobManager.get_job(..., tenant_id=...)`` predicate below is the ONLY thing
    standing between one caller and another caller's inspection jobs, and an
    unauthenticated request would silently share the single ``"anonymous"``
    bucket with every other unauthenticated request. w2v-common 0.67.0 deleted
    ``AuthSettings.default_tenant_id`` for exactly this reason — there is no
    fallback tenant left to invent, and inventing one is the bug that release
    removed.

    Three rejections, all 401, all deliberate:

    * ABSENT / blank — no identity at all (``MissingTenantError``).
    * NON-UUID — a legacy slug (``anonymous``, ``n8n-dev``) is refused here
      rather than becoming a second, un-owned tenant key in the DB and in redis.
    * The NIL uuid, rejected BY VALUE in :func:`src.tenant.require_tenant`.
      ``00000000-0000-0000-0000-000000000000`` was the old fail-safe substitute,
      it is a *valid* uuid so no type check catches it, and ``uuid.UUID(v).int == 0``
      catches every spelling — dashed, undashed, braced, ``urn:uuid:`` — where a
      string compare against the dashed literal catches exactly one.

    Returned as ``str``: ``InspectionJob.tenant_id`` is a ``String(128)`` column
    and ``job_status_key()`` builds a redis key, so the Python side holds text.
    Canonical dashed form means an undashed inbound spelling cannot become a
    second key for the same tenant.
    """
    raw = getattr(request.state, "tenant_id", None)
    try:
        return str(require_tenant(raw, source="request.state.tenant_id"))
    except MissingTenantError as exc:
        raise HTTPException(
            status_code=401,
            detail=(
                "No tenant identity on the request. app_inspector_service is "
                "tenant-scoped: authenticate with a license token carrying a tenant."
            ),
        ) from exc
    except InvalidTenantIdError as exc:
        # Deliberately does not echo the value: X-Tenant-Id can carry a
        # `Bearer <license token>` and logging it would leak the token.
        raise HTTPException(
            status_code=401,
            detail=(
                "Tenant identity on the request is not a usable tenant uuid. "
                "Legacy slug tenants and the nil uuid are not accepted."
            ),
        ) from exc


def _get_inspector_service(request: Request) -> InspectorService:
    """Retrieve the InspectorService instance from application state."""
    return request.app.state.inspector_service


def _build_screen_info(s: dict) -> ScreenInfo:
    """Build a ScreenInfo from a serialized screen dict."""
    return ScreenInfo(
        screen_id=s.get("screen_id", ""),
        screen_type=s.get("screen_type", "unknown"),
        activity_name=s.get("activity_name"),
        text_content=s.get("text_content"),
        interactive_elements=s.get("interactive_elements", 0),
        accessibility_ids=s.get("accessibility_ids", []),
        screenshot_path=s.get("screenshot_path"),
        # Absent on rows written before the field existed -> "unknown"
        # (unmeasured), never silently promoted to "captured".
        screenshot_status=s.get("screenshot_status") or "unknown",
        headings=s.get("headings", []),
        has_forms=s.get("has_forms", False),
        has_media=s.get("has_media", False),
        content_sections=[ContentSectionInfo(**cs) for cs in s.get("content_sections", [])],
        accessibility_issues=[
            AccessibilityIssueInfo(**ai) for ai in s.get("accessibility_issues", [])
        ],
        accessibility_score=s.get("accessibility_score"),
        interactive_elements_detail=[
            InteractiveElementInfo(**ie) for ie in s.get("interactive_elements_detail", [])
        ],
        # Absent on rows written before the field existed -> 0. That is a floor,
        # not a measurement: those rows dropped an unknown number of taps.
        navigation_failures=s.get("navigation_failures") or 0,
    )
