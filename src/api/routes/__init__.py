"""Assembled API router from domain sub-modules.

Route registration order matters -- literal paths must come before
parameterized ``/{job_id}`` paths to avoid FastAPI matching ``/farms``
as a job_id.
"""

from fastapi import APIRouter
from w2v_common.auth import LICENSE_GATE

from src.api.routes.compare import router as compare_router
from src.api.routes.export import router as export_router
from src.api.routes.farms import router as farms_router
from src.api.routes.inspections import router as inspections_router
from src.api.routes.jobs import router as jobs_router
from src.api.routes.selection import router as selection_router

# The aggregate router carries the ``/api/inspect`` prefix so all sub-routers
# share a single mount point. The inspections sub-router itself owns the
# literal ``POST /api/inspect`` endpoint and therefore declares the full
# prefix on its own ``APIRouter`` -- this is the only way to register a path
# operation against the bare prefix without FastAPI rejecting an empty
# (prefix, path) pair (see fastapi.routing._check_include_router).
#
# ENTITLEMENT GATE (w2v_common.auth.entitlement, >=0.75.0), mounted
# router-wide. Until now this service authenticated the licence token for
# tenant IDENTITY only (AuthMiddleware answers "who are you") and never asked
# "may you" -- a tenant whose licence is expired or revoked still carries a
# syntactically valid token and was served every /api/inspect route.
# LICENSE_GATE closes that: none/expired/revoked -> 402 license_<state>, an
# unestablishable verdict -> 503 license_unavailable + Retry-After,
# active/expiring_soon -> pass.
#
# EVERY route under this aggregate router is gated -- job creation
# (inspections), comparison, job/screen reads, screen selection, export, and
# the device-farm listing. All of it is a "usage of w2v" under the owner's
# rule ("ANY usage of w2v AND ANY subsequent service is ONLY allowed based on
# a license"); none of it is an inbound webhook or peer-to-peer callback, so
# nothing here has a reason to be exempt.
#
# DELIBERATELY NOT GATED, each for a stated reason:
#   * the health router (/health, /health/live, /health/ready, /healthz,
#     /readyz), mounted separately in src/asgi.py -- never passes through
#     this router at all. A 402/503 on a probe CrashLoops the pod, and an
#     unlicensed tenant cannot be billed by killing our own deployment. The
#     gate also refuses these paths by name (``is_license_exempt_path``), so
#     this is belt and braces, not a single point of failure.
#   * /metrics and /docs|/openapi.json, added by w2v_common's create_app: a
#     Prometheus scrape and the schema carry no tenant and do no tenant work.
#
# DORMANT ON MERGE: require_license is a no-op unless
# W2V_LICENSE_GATE_ENABLED is true, read per-call from os.environ. It
# defaults to False, so this change alters no behaviour until gitops arms
# the flag. Preserve that default.
router = APIRouter(tags=["inspect"], dependencies=list(LICENSE_GATE))

# Literal paths FIRST (before parameterized /{job_id} paths). The
# inspections router sets its own prefix so ``@router.post("")`` resolves to
# ``POST /api/inspect`` -- no trailing slash, no 307 redirect for callers.
router.include_router(inspections_router)
# All remaining sub-routers share the ``/api/inspect`` prefix.
router.include_router(compare_router, prefix="/api/inspect")  # POST "/compare" (literal)
router.include_router(farms_router, prefix="/api/inspect")  # GET "/farms", ...
# Parameterized paths AFTER.
router.include_router(jobs_router, prefix="/api/inspect")  # GET "/{job_id}", ...
router.include_router(selection_router, prefix="/api/inspect")  # /{job_id}/screens/...
router.include_router(export_router, prefix="/api/inspect")  # /{job_id}/export/{format}
