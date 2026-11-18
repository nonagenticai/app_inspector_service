"""Customer-credential leakage into logs, exceptions and persisted job errors.

``app_path`` is caller-supplied. It may carry a secret in TWO shapes:

* userinfo embedded in the URL (``https://<user>:<secret>@host/app.apk``).
  ``pin_target`` preserves
  it DELIBERATELY (the fetch needs it), so every downstream message that
  formats the URL leaks it.
* ``https://host/app.apk?X-Amz-Signature=…`` — a presigned URL carries the
  secret in the QUERY STRING, which the redaction helper originally declared
  out of contract.

Each test below pins a PRODUCTION call site, not just the helper: a fix applied
only to ``redact_url_credentials`` must not be able to make these pass.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.helpers.url_validation import SSRFError, resolve_public_ip, validate_url_safe
from src.services.app_acquisition import AppAcquisitionService, DownloadError
from src.services.inspector_service import InspectorService
from src.utils.redaction import redact_url_credentials

# Sentinels that must never survive into a log line, an exception message or a
# persisted error string.
_USERINFO_SECRET = "s3cr3t-access-key"  # pragma: allowlist secret
_SIGNATURE_SECRET = "deadbeefcafe1234"  # pragma: allowlist secret

_USERINFO_URL = f"https://bob:{_USERINFO_SECRET}@apps.example.com/app.apk"
_PRESIGNED_URL = (
    f"https://apps.example.com/app.apk?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Expires=900&X-Amz-Signature={_SIGNATURE_SECRET}"
)

_SECRETS = (_USERINFO_SECRET, _SIGNATURE_SECRET)


def _assert_clean(text: str) -> None:
    for secret in _SECRETS:
        assert secret not in text, f"credential leaked: {text!r}"


def _settings_mock() -> MagicMock:
    return MagicMock(
        acquisition=MagicMock(
            download_timeout=5,
            max_file_size_mb=500,
            temp_dir="/tmp/app_inspector_test",
            allowed_local_paths=["/tmp/app_inspector_test"],
            allowed_minio_bucket="bucket",
        ),
        storage=MagicMock(minio_bucket="bucket"),
    )


# ---------------------------------------------------------------------------
# Helper contract — both secret shapes
# ---------------------------------------------------------------------------


class TestRedactionCoversBothShapes:
    def test_userinfo_is_masked(self):
        _assert_clean(redact_url_credentials(_USERINFO_URL))

    def test_presigned_query_signature_is_masked(self):
        """A presigned URL hides its secret in the query string, not userinfo."""
        _assert_clean(redact_url_credentials(_PRESIGNED_URL))

    def test_non_secret_query_params_stay_diagnosable(self):
        out = redact_url_credentials(_PRESIGNED_URL)
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in out
        assert "apps.example.com" in out


# ---------------------------------------------------------------------------
# src/helpers/url_validation.py — SSRFError / ValueError messages
# ---------------------------------------------------------------------------


class TestUrlValidationMessages:
    def test_no_hostname_message_does_not_leak(self):
        """url_validation.py:83 — ``Cannot extract hostname from URL: {url}``."""
        with pytest.raises(ValueError) as ctx:
            validate_url_safe(f"https:///app.apk?X-Amz-Signature={_SIGNATURE_SECRET}")
        _assert_clean(str(ctx.value))

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_dns_failure_message_does_not_leak(self, mock_dns):
        """url_validation.py:96 — ``DNS resolution failed for {host}: {url}``."""
        mock_dns.side_effect = socket.gaierror("no such host")
        with pytest.raises(SSRFError) as ctx:
            validate_url_safe(_PRESIGNED_URL)
        _assert_clean(str(ctx.value))

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_blocked_address_message_does_not_leak(self, mock_dns):
        """url_validation.py:102 — ``URL {url} resolves to blocked address``."""
        mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.5", 443))]
        with pytest.raises(SSRFError) as ctx:
            validate_url_safe(_USERINFO_URL)
        _assert_clean(str(ctx.value))

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_blocked_address_message_does_not_leak_presigned(self, mock_dns):
        mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.5", 443))]
        with pytest.raises(SSRFError) as ctx:
            validate_url_safe(_PRESIGNED_URL)
        _assert_clean(str(ctx.value))

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_no_addresses_message_does_not_leak(self, mock_dns):
        """url_validation.py:133 — ``DNS returned no addresses for {url}``."""
        mock_dns.return_value = []
        with pytest.raises(SSRFError) as ctx:
            resolve_public_ip(_USERINFO_URL)
        _assert_clean(str(ctx.value))


# ---------------------------------------------------------------------------
# src/services/app_acquisition.py — download log line + DownloadError messages
# ---------------------------------------------------------------------------


class _RaisingClient:
    """Stand-in httpx.AsyncClient whose ``stream`` raises a transport error."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        raise self._exc


@pytest.mark.parametrize("url", [_USERINFO_URL, _PRESIGNED_URL])
def test_download_log_line_does_not_leak(url, caplog):
    """app_acquisition.py:481 — ``logger.info("Downloading %s -> %s", url, dest)``."""
    with patch("src.services.app_acquisition.get_settings", return_value=_settings_mock()):
        svc = AppAcquisitionService()

    exc = httpx.ConnectError("connection refused")
    with (
        patch("src.services.app_acquisition.resolve_public_ip", return_value="93.184.216.34"),
        patch(
            "src.services.app_acquisition.httpx.AsyncClient",
            _RaisingClient(exc),
        ),
        caplog.at_level(logging.DEBUG, logger="src.services.app_acquisition"),
        pytest.raises(DownloadError),
    ):
        asyncio.run(svc._download_file(url))

    for record in caplog.records:
        _assert_clean(record.getMessage())


@pytest.mark.parametrize("url", [_USERINFO_URL, _PRESIGNED_URL])
def test_transport_download_error_does_not_leak(url):
    """app_acquisition.py:545 — ``Failed to download {url}: {exc}``."""
    with patch("src.services.app_acquisition.get_settings", return_value=_settings_mock()):
        svc = AppAcquisitionService()

    with (
        patch("src.services.app_acquisition.resolve_public_ip", return_value="93.184.216.34"),
        patch(
            "src.services.app_acquisition.httpx.AsyncClient",
            _RaisingClient(httpx.ConnectError("connection refused")),
        ),
        pytest.raises(DownloadError) as ctx,
    ):
        asyncio.run(svc._download_file(url))
    _assert_clean(str(ctx.value))


@pytest.mark.parametrize("url", [_USERINFO_URL, _PRESIGNED_URL])
def test_http_status_download_error_does_not_leak(url):
    """app_acquisition.py:542 — ``HTTP {code} downloading {url}``.

    httpx embeds the (pinned, still-credentialed) request URL in its own
    ``HTTPStatusError`` string, so redacting only the ``{url}`` placeholder is
    not enough — the whole message must be scrubbed.
    """
    with patch("src.services.app_acquisition.get_settings", return_value=_settings_mock()):
        svc = AppAcquisitionService()

    request = httpx.Request("GET", url)
    response = httpx.Response(403, request=request)
    status_exc = httpx.HTTPStatusError("403 Forbidden", request=request, response=response)

    with (
        patch("src.services.app_acquisition.resolve_public_ip", return_value="93.184.216.34"),
        patch(
            "src.services.app_acquisition.httpx.AsyncClient",
            _RaisingClient(status_exc),
        ),
        pytest.raises(DownloadError) as ctx,
    ):
        asyncio.run(svc._download_file(url))
    _assert_clean(str(ctx.value))


# ---------------------------------------------------------------------------
# src/services/inspector_service.py — the PERSIST boundary
# ---------------------------------------------------------------------------


def test_handle_failure_does_not_persist_credentials(caplog):
    """inspector_service.py:371 — ``mark_failed_with_partial(..., str(exc))``.

    Whatever slips past the per-site redaction must still not reach
    ``jobs.error_message``, which ``/api/inspect/{id}`` returns verbatim.
    """
    svc = InspectorService.__new__(InspectorService)
    svc._job_manager = AsyncMock()

    # Both shapes, composed the way a raise site actually composes them: the
    # URL appears twice, once from our own placeholder and once from the
    # underlying transport error that quotes the request URL back.
    exc = DownloadError(
        f"Failed to download {_USERINFO_URL}: "
        f"ConnectError(request url={_PRESIGNED_URL}) after redirect to {_USERINFO_URL}"
    )

    with caplog.at_level(logging.DEBUG, logger="src.services.inspector_service"):
        asyncio.run(
            svc._handle_failure(
                AsyncMock(), "job-1", "01950000-0000-7000-8000-000000000001", exc, [], []
            )
        )

    call = svc._job_manager.mark_failed_with_partial.await_args
    persisted = call.args[2] if len(call.args) > 2 else call.kwargs["error_message"]
    _assert_clean(persisted)

    for record in caplog.records:
        _assert_clean(record.getMessage())


# ---------------------------------------------------------------------------
# src/api/routes/inspections.py — the submit-time log line
# ---------------------------------------------------------------------------


def test_start_inspection_log_does_not_leak(caplog):
    """inspections.py:183 — logs the caller-supplied ``app_path`` verbatim."""
    from src.api.routes import inspections
    from src.api.schemas import InspectRequest

    body = InspectRequest(app_path=_USERINFO_URL, platform="android")
    service = MagicMock()
    service.create_job = AsyncMock(return_value="job-1")
    db = AsyncMock()

    with (
        patch.object(
            inspections, "_get_tenant_id", return_value="01950000-0000-7000-8000-000000000001"
        ),
        patch.object(inspections, "_get_inspector_service", return_value=service),
        patch.object(inspections.asyncio, "create_task", MagicMock()),
        caplog.at_level(logging.DEBUG, logger="src.api.routes.inspections"),
    ):
        asyncio.run(inspections.start_inspection(body, MagicMock(), db))

    for record in caplog.records:
        _assert_clean(record.getMessage())


# ── The startup "Database connected" line must never carry the DSN password ──
#
# REGRESSION: ``_init_database`` logged ``settings.database.url`` raw -- the full
# postgresql+psycopg://<user>:<password>@host:port/db DSN -- at INFO on the
# connect path, so the live app_inspector_service password landed in this pod's
# stdout and in anything shipping those logs, indefinitely.
#
# This drives the REAL ``_init_database`` rather than the redactor, because the
# defect is at the CALL SITE: a test that only exercises
# ``w2v_common.observability.database_endpoint`` stays green for the entire
# duration of the leak. That helper has its own unit tests in w2v_common; this
# one exists to fail if anyone hands the raw url back to the logger here.

# Wholly synthetic and shares no prefix with any real credential -- this string
# is committed, printed by pytest on failure and ends up in CI logs, i.e.
# exactly the places this change exists to keep secrets out of.
_FAKE_PASSWORD = "not-a-real-password-xyzzy"  # pragma: allowlist secret
_FAKE_DSN = (
    f"postgresql+psycopg://inspector_app:{_FAKE_PASSWORD}"
    "@pgbouncer.pgbouncer.svc.cluster.local:6432/app_inspector_service"
)


async def test_startup_db_log_does_not_leak_the_dsn_password(monkeypatch):
    """The emitted startup log record must not contain the DSN password."""
    import io

    from fastapi import FastAPI

    from src import asgi

    monkeypatch.setattr(asgi.settings.database, "url", _FAKE_DSN)

    # A handler attached directly to the module logger, NOT ``caplog``: the
    # service reconfigures root logging at import time, so a root-level captor
    # can observe nothing and every assertion below would pass vacuously.
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.INFO)
    asgi.logger.addHandler(handler)
    # ``setLevel`` and not ``logger.level = ...``: Logger caches isEnabledFor
    # results, and only setLevel clears that cache. Assigning the attribute
    # leaves a stale "INFO is disabled" entry from an earlier record and the
    # line never reaches the handler at all.
    previous_level = asgi.logger.level
    asgi.logger.setLevel(logging.INFO)

    app = FastAPI()
    try:
        with (
            patch.object(asgi, "get_engine", new_callable=AsyncMock, return_value=AsyncMock()),
            # Installs a SQLAlchemy event listener process-wide; irrelevant here
            # and must not leak into other tests.
            patch("src.database.rls_events.install_rls_tenant_rebind", MagicMock()),
        ):
            await asgi._init_database(app)
    finally:
        asgi.logger.removeHandler(handler)
        asgi.logger.setLevel(previous_level)

    captured = buffer.getvalue()
    # Guard against a vacuous pass: if the line never emitted, the assertions
    # below would hold for the wrong reason.
    assert "Database connected" in captured, (
        "the 'Database connected' line never emitted; the assertions would be vacuous"
    )
    assert _FAKE_PASSWORD not in captured, "DSN password leaked into the startup log"
    assert "inspector_app" not in captured, "DSN username leaked into the startup log"
    # Still useful, or the next person deletes the log line instead of keeping it.
    assert "pgbouncer.pgbouncer.svc.cluster.local:6432/app_inspector_service" in captured


# ── The startup "Redis connected" line must never carry the Redis password ──
#
# REGRESSION: ``_init_redis`` logged ``settings.redis.url`` raw -- the full
# redis://[user]:<password>@host:port/db URL -- at INFO on the connect path, so
# the live Redis password landed in this pod's stdout and in anything shipping
# those logs, on every single pod start.
#
# Same shape as the DSN case above, and the same reasoning: this drives the REAL
# ``_init_redis`` rather than ``w2v_common.observability.redis_endpoint``,
# because the defect is at the CALL SITE. A test of the helper alone stays green
# for the entire duration of the leak, and that helper already has its own unit
# tests in w2v_common.

_FAKE_REDIS_URL = f"redis://default:{_FAKE_PASSWORD}@redis.w2v.svc.cluster.local:6379/3"


async def test_startup_redis_log_does_not_leak_the_redis_password(monkeypatch):
    """The emitted "Redis connected" record must not contain the URL password."""
    import io

    from fastapi import FastAPI

    from src import asgi

    monkeypatch.setattr(asgi.settings.redis, "url", _FAKE_REDIS_URL)

    # Own handler on the module logger, NOT ``caplog``: this service
    # reconfigures root logging at import time (basicConfig +
    # clamp_credential_leaking_loggers), so a root-level captor can observe
    # nothing and every assertion below would pass vacuously.
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.INFO)
    asgi.logger.addHandler(handler)
    # ``setLevel`` and not ``logger.level = ...``: Logger caches isEnabledFor
    # results and only setLevel clears that cache. Assigning the attribute
    # leaves a stale "INFO is disabled" entry, the record never reaches the
    # handler, and the negative assertions below pass on EMPTY output while the
    # leak is still live.
    previous_level = asgi.logger.level
    asgi.logger.setLevel(logging.INFO)

    app = FastAPI()
    redis_client = AsyncMock()
    try:
        with (
            patch.object(asgi, "init_redis", new_callable=AsyncMock, return_value=redis_client),
            patch.object(asgi, "get_redis_client", MagicMock(return_value=redis_client)),
        ):
            await asgi._init_redis(app)
    finally:
        asgi.logger.removeHandler(handler)
        asgi.logger.setLevel(previous_level)

    captured = buffer.getvalue()
    # Guard against a vacuous pass: if the line never emitted, the assertions
    # below would hold for the wrong reason.
    assert "Redis connected" in captured, (
        "the 'Redis connected' line never emitted; the assertions would be vacuous"
    )
    assert _FAKE_PASSWORD not in captured, "Redis password leaked into the startup log"
    assert "default:" not in captured, "Redis userinfo leaked into the startup log"
    # Still useful, or the next person deletes the log line instead of keeping it.
    assert "redis.w2v.svc.cluster.local:6379" in captured
