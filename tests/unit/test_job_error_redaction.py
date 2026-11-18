"""Device-farm credentials must never reach ``jobs.error_message`` or the status API.

A BrowserStack/Appium failure can quote the tenant's access key in every shape
a transport knows: bare in prose, as ``user:key``, inside a hub URL's userinfo,
as an HTTP Basic header (``base64(user:key)``), or percent-encoded. The status
endpoint returns ``error_message`` verbatim to the orchestrator, whose
``poll_job`` logs the full response — so the scrub must happen before persist.

The end-to-end case drives the REAL production path: ``_process_inspection_bg``
→ ``JobManager.mark_failed`` → postgres → ``GET /api/inspect/{id}``.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import time
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from starlette.middleware.base import BaseHTTPMiddleware
from w2v_common.cache.redis_client import RedisClient
from w2v_common.testing import inject_fakeredis

from src.api.constants import MAX_ERROR_LENGTH
from src.services.device_farm import DeviceFarmCredentials
from src.services.inspector_service import InspectorService
from src.services.job_manager import JobManager
from src.utils.redaction import redact_error_message
from tests.conftest import TENANT_APP

_USER = "acme_bsuser42"
# Contains characters that percent-encode, so the encoded form differs from raw.
_KEY = "Zx9/kQ+v7Lm=pR2tW8"  # pragma: allowlist secret
_CREDS = DeviceFarmCredentials(username=_USER, access_key=SecretStr(_KEY))

_B64_PAIR = base64.b64encode(f"{_USER}:{_KEY}".encode()).decode()
_ENCODED_KEY = quote(_KEY, safe="")

# Forms that must not survive in any persisted/returned string.
_FORBIDDEN = (
    _KEY,
    f"{_USER}:{_KEY}",
    _B64_PAIR,
    _B64_PAIR.rstrip("="),
    _ENCODED_KEY,
    _USER,
)

# Ordinary diagnostic text that MUST survive.
_PRESERVED = ("Appium session failed", "hub-cloud.browserstack.com", "status 401")


def _leaky_message() -> str:
    return (
        f"Appium session failed: status 401 for key {_KEY} "
        f"(creds {_USER}:{_KEY}) at https://{_USER}:{_KEY}@hub-cloud.browserstack.com/wd/hub "
        f"header Authorization: Basic {_B64_PAIR} encoded={_ENCODED_KEY} "
        f"unpadded={_B64_PAIR.rstrip('=')}"
    )


def _assert_clean(text: str | None) -> None:
    assert text is not None
    for secret in _FORBIDDEN:
        assert secret not in text, f"credential form {secret!r} leaked: {text!r}"
    for keep in _PRESERVED:
        assert keep in text, f"diagnostic text {keep!r} was destroyed: {text!r}"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def test_helper_scrubs_every_known_credential_form():
    _assert_clean(redact_error_message(_leaky_message(), username=_USER, access_key=_KEY))


def test_helper_without_known_creds_still_masks_key_names_and_basic_auth():
    """Generic layer (used inside JobManager): key=value and Basic/Bearer values."""
    msg = (
        f"farm error: browserstack_access_key={_KEY}, "
        f'{{"access_key": "{_KEY}"}} Authorization: Basic {_B64_PAIR} '
        f"Bearer abcdefghijklmnop at https://{_USER}:{_KEY}@hub-cloud.browserstack.com/wd/hub"
    )
    out = redact_error_message(msg)
    assert _KEY not in out
    assert _B64_PAIR not in out
    assert "abcdefghijklmnop" not in out
    assert "browserstack_access_key=***" in out
    assert "hub-cloud.browserstack.com" in out


def test_helper_masks_json_escaped_unknown_key():
    """A JSON body quoted inside a repr arrives with backslash-escaped quotes."""
    msg = f'farm said: {{\\"browserstack_access_key\\": \\"{_KEY}\\", \\"ok\\": 1}}'
    out = redact_error_message(msg)
    assert _KEY not in out
    assert "browserstack_access_key" in out


@pytest.mark.parametrize(
    "adversarial",
    [
        "a" * 200_000,
        "a" * 200_000 + "://",
        "Basic " + "A" * 200_000,
        "x://" + "a" * 200_000,
        "access_key=" + "k" * 200_000,
    ],
    ids=["letter-run", "letter-run-scheme", "basic-run", "userinfo-run", "kv-run"],
)
def test_helper_is_linear_on_adversarial_input(adversarial):
    """Runs on str(exc) inside the event loop: must not be quadratic.

    The unbounded scheme pattern took 25.6 s on 200k chars before the fix.
    """
    start = time.perf_counter()
    out = redact_error_message(adversarial, username=_USER, access_key=_KEY)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2, f"{elapsed:.3f}s on {len(adversarial)} chars"
    assert len(out) <= 2 * MAX_ERROR_LENGTH


def test_helper_preserves_ordinary_error_text():
    for msg in (
        "Connection refused",
        "Download failed: HTTP 404",
        "max_tokens=5 exceeded; password_policy weak; basic auth required",
        "device farm credentials unavailable for provider 'browserstack' -- resubmit "
        "with browserstack_username and browserstack_access_key",
    ):
        assert redact_error_message(msg) == msg


# ---------------------------------------------------------------------------
# inspector_service._handle_failure — persist + log
# ---------------------------------------------------------------------------


async def test_handle_failure_scrubs_known_farm_credentials(caplog):
    svc = InspectorService.__new__(InspectorService)
    svc._job_manager = AsyncMock()

    with caplog.at_level(logging.DEBUG, logger="src.services.inspector_service"):
        await svc._handle_failure(
            AsyncMock(),
            "job-1",
            TENANT_APP,
            RuntimeError(_leaky_message()),
            [],
            [],
            farm_credentials=_CREDS,
        )

    call = svc._job_manager.mark_failed_with_partial.await_args
    _assert_clean(call.args[2])
    for record in caplog.records:
        for secret in _FORBIDDEN:
            assert secret not in record.getMessage()


# ---------------------------------------------------------------------------
# End to end: _process_inspection_bg → postgres → GET /api/inspect/{id}
# ---------------------------------------------------------------------------


class _TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.tenant_id = TENANT_APP
        return await call_next(request)


@pytest.fixture
async def client(db_session, monkeypatch):
    redis_client = RedisClient(redis_url="redis://localhost:6379/0")
    inject_fakeredis(redis_client)
    monkeypatch.setattr("src.api.routes.jobs.get_redis_client", lambda: redis_client)

    from src.api.routes import router as api_router
    from src.database.session import get_db

    app = FastAPI(redirect_slashes=False)
    app.add_middleware(_TenantMiddleware)
    app.include_router(api_router)

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


async def test_bg_failure_never_persists_or_returns_farm_credentials(
    db_session, db_session_factory, client, monkeypatch, caplog
):
    from src.api.routes import inspections

    job = await JobManager.create_job(
        db_session,
        tenant_id=TENANT_APP,
        app_path="/tmp/a.apk",
        platform="android",
        device_farm_provider="browserstack",
    )
    await db_session.commit()

    @contextlib.asynccontextmanager
    async def _session():
        async with db_session_factory() as s:
            yield s

    monkeypatch.setattr(inspections, "get_db_session", _session)
    service = AsyncMock()
    service.process_job = AsyncMock(side_effect=RuntimeError(_leaky_message()))

    with caplog.at_level(logging.DEBUG, logger="src.api.routes.inspections"):
        await inspections._process_inspection_bg(
            service, job.job_id, TENANT_APP, farm_credentials=_CREDS
        )

    # Persisted row (fresh session, not the identity-mapped object).
    async with db_session_factory() as s:
        stored = await JobManager.get_job(s, job.job_id, tenant_id=TENANT_APP)
    assert stored is not None
    assert stored.status == "failed"
    _assert_clean(stored.error_message)

    # Log line.
    for record in caplog.records:
        for secret in _FORBIDDEN:
            assert secret not in record.getMessage()

    # Status API body. The route reads through the test's own session, whose
    # identity map still holds the pre-failure object -- drop it.
    job_id = job.job_id
    db_session.expire_all()
    resp = await client.get(f"/api/inspect/{job_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    _assert_clean(body["error"])
    for secret in _FORBIDDEN:
        assert secret not in resp.text


async def test_status_api_scrubs_a_legacy_row_written_before_the_fix(db_session, client):
    """A row persisted unredacted by an older build must not be returned raw."""
    job = await JobManager.create_job(db_session, tenant_id=TENANT_APP, app_path="/tmp/b.apk")
    job.status = "failed"
    job.error_message = (
        f"session failed: access_key={_KEY} Authorization: Basic {_B64_PAIR} "
        f"https://{_USER}:{_KEY}@hub-cloud.browserstack.com/wd/hub"
    )
    await db_session.commit()

    resp = await client.get(f"/api/inspect/{job.job_id}")
    assert resp.status_code == 200, resp.text
    assert _KEY not in resp.text
    assert _B64_PAIR not in resp.text
    assert "session failed" in resp.json()["error"]
