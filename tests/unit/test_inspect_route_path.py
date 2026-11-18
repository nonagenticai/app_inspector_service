"""Regression test for the ``POST /api/inspect`` route path.

Pre-existing bug: ``@router.post("/")`` on a router with prefix ``/api/inspect``
produced ``POST /api/inspect/`` and FastAPI's ``redirect_slashes=True`` would
return a 307 for callers that posted to ``/api/inspect`` (no trailing slash).
``BaseServiceClient`` (orchestrator + bdd) does not follow redirects, so the
empty redirect body bubbled up as a ``JSONDecodeError`` in production.

This test pins the route to the no-trailing-slash form so that the regression
cannot reappear without breaking the test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from src.api.routes.inspections import router as inspections_router
from src.database.session import get_db
from tests.conftest import attach_test_tenant


@pytest.fixture
def inspect_app() -> FastAPI:
    """Build a minimal FastAPI app exposing only the inspections router.

    Avoids the full ``src.asgi:app`` lifespan (which requires Postgres + Redis)
    by mounting just the router under test and stubbing the application-state
    services that the handler retrieves via ``request.app.state``.
    """
    app = FastAPI()
    app.include_router(inspections_router)

    # The handler reads ``request.app.state.inspector_service`` directly
    # (not via Depends), so seed app state with an async-aware stub.
    inspector_service = MagicMock()
    inspector_service.create_job = AsyncMock(return_value="00000000-0000-7000-8000-000000000001")
    app.state.inspector_service = inspector_service

    db_session = AsyncMock()
    db_session.commit = AsyncMock()

    async def _override_db() -> Any:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    # This app mounts the router WITHOUT w2v_common's AuthMiddleware, so nothing
    # would bind request.state.tenant_id and the handler would 401 before it
    # could answer the routing question this test asks.
    return attach_test_tenant(app)


@pytest.mark.asyncio
async def test_post_inspect_no_trailing_slash_does_not_307(inspect_app: FastAPI) -> None:
    """POST /api/inspect (no slash) must NOT 307 -- regression for #1."""
    transport = httpx.ASGITransport(app=inspect_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        response = await client.post(
            "/api/inspect",
            json={"app_path": "https://example.com/app.apk", "platform": "android"},
        )
    assert response.status_code != 307, (
        f"POST /api/inspect returned {response.status_code} -- the route is "
        "declared with a trailing slash again, which causes BaseServiceClient "
        "to fail with JSONDecodeError on the empty redirect body."
    )
    # The handler should succeed (200) with our stubs in place.
    assert response.status_code == 200, response.text
    body = response.json()
    assert "job_id" in body


def test_inspect_route_registered_without_trailing_slash() -> None:
    """The router must register the literal path ``/api/inspect`` (no slash)."""
    inspect_post_paths = {
        route.path
        for route in inspections_router.routes
        # ``APIRoute.methods`` is Optional in starlette's types; None means the
        # route declares no methods, which can never match POST.
        if isinstance(route, APIRoute) and route.methods and "POST" in route.methods
    }
    assert "/api/inspect" in inspect_post_paths, (
        f"POST /api/inspect not registered. Found: {inspect_post_paths}. "
        "If this test fails, someone re-introduced @router.post('/') under "
        "prefix='/api/inspect', producing /api/inspect/ and the 307 bug."
    )
    assert "/api/inspect/" not in inspect_post_paths, (
        f"POST /api/inspect/ (with trailing slash) is registered: {inspect_post_paths}. "
        "Use @router.post('') -- not @router.post('/') -- under the /api/inspect prefix."
    )
