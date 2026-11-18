"""``app_path`` confinement on ``POST /api/inspect``.

``w2v_common.storage.StorageService`` has no tenant concept: handed a bucket +
key it fetches. A caller-supplied ``minio://bucket/key`` app_path would
therefore let tenant A name ANY object in ANY bucket the service's MinIO
credentials can read — IF minio:// acquisition were wired up. It is not: no
caller in ``src/`` passes ``minio_storage`` to ``AppAcquisitionService``, so
such an app_path already died in the background job with "MinIO storage is not
configured". This guard is defence-in-depth ahead of that wiring, plus one real
behaviour change: a bad app_path now 400s at submit instead of 202-then-fail.

These tests pin the confinement rule: ONE configured bucket, and an object key
under the caller's own ``<tenant_id>/`` prefix compared as path SEGMENTS — with
the key returned in its RAW (percent-encoded) form, since decoding decides
safety but must not rewrite which object is named.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from src.api.routes import inspections as inspections_route
from src.api.routes.inspections import _validate_app_path
from src.database.session import get_db

_BUCKET = "w2v-app-inspector"
_TENANT = "e148e8af-0000-4000-8000-000000000001"
_OTHER_TENANT = "ffffffff-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Pin the allowed bucket + local dirs without touching the real env."""
    settings = MagicMock(
        acquisition=MagicMock(
            allowed_minio_bucket=_BUCKET,
            allowed_local_paths=[str(tmp_path)],
        ),
        storage=MagicMock(minio_bucket=_BUCKET),
    )
    monkeypatch.setattr(inspections_route, "get_settings", lambda: settings)


# ---------------------------------------------------------------------------
# minio:// confinement
# ---------------------------------------------------------------------------


def test_own_tenant_object_is_accepted_and_canonicalized() -> None:
    """The legitimate own-tenant path still works (and is normalised)."""
    result = _validate_app_path(f"minio://{_BUCKET}/{_TENANT}/uploads/./app.apk", tenant_id=_TENANT)
    assert result == f"minio://{_BUCKET}/{_TENANT}/uploads/app.apk"


def test_percent_encoded_key_is_persisted_verbatim() -> None:
    """A legitimate ``%``-bearing key must survive validation byte-for-byte.

    ``%`` is legal in a MinIO object key. Kills the mutation where
    ``confine_minio_uri`` returns the percent-DECODED key: the value persisted
    on the job (and later fetched) would become ``.../My App.apk``, a different
    object. Decoding decides safety; it must not rewrite the identifier.
    """
    uri = f"minio://{_BUCKET}/{_TENANT}/uploads/My%20App.apk"
    assert _validate_app_path(uri, tenant_id=_TENANT) == uri


@pytest.mark.parametrize(
    ("app_path", "reason"),
    [
        (f"minio://w2v-audiobooks/{_TENANT}/app.apk", "foreign bucket"),
        (f"minio://{_BUCKET}/{_OTHER_TENANT}/app.apk", "foreign tenant prefix"),
        (f"minio://{_BUCKET}/{_TENANT}/../{_OTHER_TENANT}/app.apk", "dotdot escape"),
        (f"minio://{_BUCKET}/{_TENANT}evil/app.apk", "sibling prefix"),
        (f"minio://{_BUCKET}/{_TENANT}/%2e%2e/{_OTHER_TENANT}/app.apk", "encoded escape"),
        (
            f"minio://{_BUCKET}/{_TENANT}/%252e%252e%252f{_OTHER_TENANT}/app.apk",
            "double-encoded escape",
        ),
        (f"minio://{_BUCKET}//{_OTHER_TENANT}/app.apk", "absolute key"),
        (f"s3://{_BUCKET}/{_TENANT}/app.apk", "s3 scheme"),
        ("file:///etc/passwd", "file scheme"),
    ],
)
def test_escapes_are_rejected(app_path: str, reason: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_app_path(app_path, tenant_id=_TENANT)
    assert exc.value.status_code == 400, reason


def test_http_urls_still_pass_through() -> None:
    url = "https://example.com/app.apk"
    assert _validate_app_path(url, tenant_id=_TENANT) == url


def test_local_path_within_allowed_dir_still_works(tmp_path) -> None:
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK\x03\x04")
    assert _validate_app_path(str(apk), tenant_id=_TENANT) == str(apk)


def test_local_path_outside_allowed_dir_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_app_path("/etc/passwd", tenant_id=_TENANT)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Through the route (proves the guard is wired to the caller's tenant)
# ---------------------------------------------------------------------------


def _app(tenant_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(inspections_route.router)

    @app.middleware("http")
    async def _bind_tenant(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.tenant_id = tenant_id
        return await call_next(request)

    inspector_service = MagicMock()
    inspector_service.create_job = AsyncMock(return_value="00000000-0000-7000-8000-000000000001")
    app.state.inspector_service = inspector_service

    db_session = AsyncMock()
    db_session.commit = AsyncMock()

    async def _override_db() -> Any:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


async def _post(app: FastAPI, app_path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/api/inspect", json={"app_path": app_path, "platform": "android"})


@pytest.mark.asyncio
async def test_route_rejects_other_tenants_object() -> None:
    app = _app(_TENANT)
    response = await _post(app, f"minio://{_BUCKET}/{_OTHER_TENANT}/app.apk")
    assert response.status_code == 400, response.text
    # The job must never be created for a rejected path.
    app.state.inspector_service.create_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_accepts_own_tenant_object() -> None:
    app = _app(_TENANT)
    response = await _post(app, f"minio://{_BUCKET}/{_TENANT}/app.apk")
    assert response.status_code == 200, response.text
    kwargs = app.state.inspector_service.create_job.await_args.kwargs
    assert kwargs["app_path"] == f"minio://{_BUCKET}/{_TENANT}/app.apk"
    assert kwargs["tenant_id"] == _TENANT
