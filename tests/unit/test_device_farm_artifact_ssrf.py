"""Credential-exfiltration + SSRF guard on the BrowserStack artifact download.

The defect these tests pin (``BrowserStackFarm.get_video``):

1. ``video_url`` was read straight out of the vendor's JSON *response body* and
   handed to ``client.get()`` with no scheme check, no host allowlist and no
   private/CGNAT check -- a full SSRF primitive driven by a response body.
2. Worse, it reused the SAME ``AsyncClient`` that was constructed with
   ``auth=(username, access_key)``. httpx generates the HTTP Basic
   ``Authorization`` header per request regardless of host, so a tampered
   upstream response redirected the **BrowserStack credentials** to an attacker.

⚠️ Mocking note: an ``AsyncMock`` client passes these assertions whether or not
the fix is present -- it never builds a real ``httpx.Request``, so there are no
real headers to inspect. Neither ``respx`` nor ``pytest-httpx`` is a dev
dependency of this repo, so we use httpx's own ``MockTransport`` (the same
device the existing ``tests/unit/test_dns_rebind_pin.py`` uses). The full httpx
request pipeline -- including the auth flow that stamps ``Authorization`` --
runs; we capture the ACTUAL outbound ``httpx.Request`` objects and assert on
their real headers and real target host.

Production lines whose removal makes a test fail are named per test.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from src.helpers.url_validation import SSRFError
from src.services.device_farm import (
    ArtifactTooLargeError,
    ArtifactUrlError,
    BrowserStackFarm,
    _validate_artifact_url,
)

_SESSION_HOST = "api-cloud.browserstack.com"
_VIDEO_HOST = "automate-sessions.browserstack.com"
_VIDEO_URL = f"https://{_VIDEO_HOST}/s3/session-123/video.mp4"
_PUBLIC_IP = "93.184.216.34"
_EVIL_IP = "203.0.113.7"  # TEST-NET-3: routable/public, so ONLY the allowlist can refuse it
_VIDEO_BYTES = b"BROWSERSTACK-VIDEO-BYTES"

# Deliberately public IPs for the attacker hosts: if a test still refuses them,
# it is the host ALLOWLIST doing the work, not the private-range check.
_DNS: dict[str, str] = {
    _SESSION_HOST: _PUBLIC_IP,
    _VIDEO_HOST: _PUBLIC_IP,
    "evil.example": _EVIL_IP,
    "browserstack.com.evil.example": _EVIL_IP,
    # Allowlisted name that resolves INTO the estate's tailnet (RFC 6598 CGNAT
    # 100.64.0.0/10 -- which is NOT ``ipaddress.is_private``).
    "internal-cdn.browserstack.com": "100.64.0.5",
    # AWS-shaped hosts: all resolve PUBLIC, so only the allowlist can decide.
    "evil-attacker-bucket.s3.amazonaws.com": _EVIL_IP,
    "evil-attacker-bucket.s3.eu-west-1.amazonaws.com": _EVIL_IP,
    "ec2-1-2-3-4.compute-1.amazonaws.com": _EVIL_IP,
    "evil.cloudfront.net": _EVIL_IP,
    "evil.automate-prod-recordings.s3.amazonaws.com": _EVIL_IP,
    "automate-prod-recordings.s3.amazonaws.com": _PUBLIC_IP,
    "app-automate-prod-recordings.s3.eu-west-1.amazonaws.com": _PUBLIC_IP,
}


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed hostname -> IP map; unmapped hosts raise ``gaierror`` (fail-closed)."""

    def fake_getaddrinfo(host: str, *_args: Any, **_kwargs: Any) -> list[Any]:
        ip = _DNS.get(host, host if _is_ip_literal(host) else None)
        if ip is None:
            raise socket.gaierror(f"unmapped host {host}")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr("src.helpers.url_validation.socket.getaddrinfo", fake_getaddrinfo)


def _is_ip_literal(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class _Capture:
    """Records every real outbound ``httpx.Request`` the transport is asked to send."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def host_of(self, request: httpx.Request) -> str:
        """Logical host: pinned requests carry the IP in the URL, name in ``Host``."""
        return request.headers.get("host", request.url.host).split(":")[0]

    @property
    def artifact_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if self.host_of(r) != _SESSION_HOST]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    video_url: str | None = _VIDEO_URL,
    redirect_to: str | None = None,
    artifact_response: Callable[[httpx.Request], httpx.Response] | None = None,
) -> _Capture:
    """Patch ``httpx.AsyncClient`` to route through a recording ``MockTransport``."""
    capture = _Capture()
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        capture.requests.append(request)
        host = capture.host_of(request)
        if host == _SESSION_HOST:
            session = {"video_url": video_url} if video_url is not None else {}
            return httpx.Response(200, json={"automation_session": session})
        if artifact_response is not None:
            return artifact_response(request)
        if host == _VIDEO_HOST and redirect_to is not None:
            return httpx.Response(302, headers={"location": redirect_to})
        if host == _VIDEO_HOST:
            return httpx.Response(200, content=_VIDEO_BYTES)
        # Any other host means the guard let an attacker target through.
        return httpx.Response(200, content=b"PWNED")

    transport = httpx.MockTransport(handler)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return capture


def _farm() -> BrowserStackFarm:
    return BrowserStackFarm(username="bs-user", access_key="bs-access-key")


def _assert_session_call_was_authenticated(capture: _Capture) -> None:
    """Anti-vacuity guard.

    Every 'no Authorization header leaked' assertion below is worthless unless
    this transport actually observes Authorization headers when they ARE sent.
    The legitimate call to the vendor API must carry HTTP Basic.
    """
    session_reqs = [r for r in capture.requests if capture.host_of(r) == _SESSION_HOST]
    assert session_reqs, "no request reached the BrowserStack session API"
    assert session_reqs[0].headers.get("authorization", "").startswith("Basic ")


def _assert_no_credentials_left_the_vendor_api(capture: _Capture) -> None:
    for request in capture.artifact_requests:
        assert "authorization" not in request.headers, (
            f"BrowserStack credentials leaked to {capture.host_of(request)}"
        )


# ---------------------------------------------------------------------------
# Positive control -- the legitimate vendor path must still work
# ---------------------------------------------------------------------------


async def test_legitimate_vendor_url_still_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real BrowserStack artifact URL downloads, credential-free and IP-pinned.

    FAILS if ``get_video``'s ``return await self._download_artifact(video_url)``
    is removed, or if ``BROWSERSTACK_ARTIFACT_HOST_SUFFIXES`` stops covering
    ``browserstack.com``.
    """
    capture = _install(monkeypatch)

    video = await _farm().get_video("session-123")

    assert video == _VIDEO_BYTES
    _assert_session_call_was_authenticated(capture)
    assert len(capture.artifact_requests) == 1
    artifact = capture.artifact_requests[0]
    # No credentials on the artifact fetch -- this is the whole point.
    assert "authorization" not in artifact.headers
    # Pinned to the IP the SSRF guard validated; Host + SNI keep the real name.
    assert artifact.url.host == _PUBLIC_IP
    assert artifact.headers["host"] == _VIDEO_HOST
    assert artifact.extensions.get("sni_hostname") == _VIDEO_HOST


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_non_allowlisted_host_refused_and_no_credentials_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tampered ``video_url`` on a foreign host is never fetched at all.

    ``evil.example`` resolves PUBLIC, so only the allowlist can refuse it.
    FAILS if the allowlist branch (``Artifact host not allowlisted``) is
    removed from ``_validate_artifact_url``.
    """
    capture = _install(monkeypatch, video_url="https://evil.example/video.mp4")

    assert await _farm().get_video("session-123") is None

    _assert_session_call_was_authenticated(capture)
    assert capture.artifact_requests == []
    _assert_no_credentials_left_the_vendor_api(capture)


async def test_redirect_off_the_vendor_host_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 302 from a legitimate BrowserStack URL to ``http://evil.example/`` is refused.

    FAILS if ``follow_redirects=False`` is dropped from ``_download_artifact``'s
    client, or if the per-hop ``current_ip = _validate_artifact_url(current_url)``
    re-check inside the redirect loop is removed.
    """
    capture = _install(monkeypatch, redirect_to="http://evil.example/")

    assert await _farm().get_video("session-123") is None

    _assert_session_call_was_authenticated(capture)
    # The first hop (allowlisted vendor host) is legitimate; nothing follows it.
    assert [capture.host_of(r) for r in capture.artifact_requests] == [_VIDEO_HOST]
    _assert_no_credentials_left_the_vendor_api(capture)


async def test_userinfo_confusion_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``http://user:pass@browserstack.com@evil.example/`` -- authority is ``evil.example``.  # pragma: allowlist secret

    FAILS if the ``parsed.username or parsed.password`` branch is removed
    (the allowlist alone would also catch this one, but userinfo must be
    refused outright: ``pin_target`` replays it to the connect target).
    """
    hostile = "http://user:pass@browserstack.com@evil.example/video.mp4"  # pragma: allowlist secret
    capture = _install(monkeypatch, video_url=hostile)

    assert await _farm().get_video("session-123") is None

    assert capture.artifact_requests == []
    _assert_no_credentials_left_the_vendor_api(capture)

    with pytest.raises(ArtifactUrlError, match="userinfo"):
        _validate_artifact_url(hostile)


@pytest.mark.parametrize(
    "hostile_url",
    [
        "http://127.0.0.1/video.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.10/video.mp4",
        "http://[::1]/video.mp4",
        "http://[fd00::1]/video.mp4",
        "http://[::ffff:169.254.169.254]/video.mp4",
        "http://100.64.0.5/video.mp4",  # CGNAT == this estate's tailnet
    ],
)
async def test_private_and_ipv6_literals_are_refused(
    monkeypatch: pytest.MonkeyPatch, hostile_url: str
) -> None:
    """Literal internal targets, IPv4 and IPv6 forms, are never fetched.

    FAILS if ``_validate_artifact_url`` stops being called before the request.
    """
    capture = _install(monkeypatch, video_url=hostile_url)

    assert await _farm().get_video("session-123") is None

    assert capture.artifact_requests == []
    _assert_no_credentials_left_the_vendor_api(capture)
    with pytest.raises((ArtifactUrlError, SSRFError)):
        _validate_artifact_url(hostile_url)


async def test_allowlisted_host_resolving_into_the_tailnet_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allowlist alone is not enough: an allowlisted NAME can resolve to CGNAT.

    FAILS if the ``return resolve_public_ip(url)`` line is dropped from
    ``_validate_artifact_url`` (the allowlist would pass this host).
    """
    hostile = "https://internal-cdn.browserstack.com/video.mp4"
    capture = _install(monkeypatch, video_url=hostile)

    assert await _farm().get_video("session-123") is None

    assert capture.artifact_requests == []
    with pytest.raises(SSRFError, match="blocked address"):
        _validate_artifact_url(hostile)


async def test_suffix_confusion_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``browserstack.com.evil.example`` must not satisfy the allowlist.

    FAILS if the allowlist check degrades to a substring/``startswith`` match
    instead of ``host == suffix or host.endswith("." + suffix)``.
    """
    capture = _install(monkeypatch, video_url="https://browserstack.com.evil.example/v.mp4")

    assert await _farm().get_video("session-123") is None

    assert capture.artifact_requests == []
    _assert_no_credentials_left_the_vendor_api(capture)


@pytest.mark.parametrize(
    "hostile_url",
    [
        "file:///etc/passwd",
        "gopher://browserstack.com/_x",
        "ftp://browserstack.com/video.mp4",
    ],
)
def test_non_http_schemes_are_refused(hostile_url: str) -> None:
    """FAILS if the ``_ARTIFACT_ALLOWED_SCHEMES`` check is removed."""
    with pytest.raises(ArtifactUrlError, match="scheme not allowed"):
        _validate_artifact_url(hostile_url)


async def test_missing_video_url_makes_no_artifact_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``video_url`` in the vendor payload -> ``None``, no second request."""
    capture = _install(monkeypatch, video_url=None)

    assert await _farm().get_video("session-123") is None
    assert capture.artifact_requests == []


# ---------------------------------------------------------------------------
# Hole 1 -- the ``amazonaws.com`` wildcard that defeated the allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_url",
    [
        # Anybody with a free AWS account owns this hostname.
        "https://evil-attacker-bucket.s3.amazonaws.com/payload.mp4",
        "https://evil-attacker-bucket.s3.eu-west-1.amazonaws.com/payload.mp4",
        # Any EC2 instance, i.e. any machine the attacker rents for an hour.
        "https://ec2-1-2-3-4.compute-1.amazonaws.com/x.mp4",
        # Any CloudFront distribution -- deliberately NOT allowlisted.
        "https://evil.cloudfront.net/x.mp4",
        # Bucket-label confusion: greedy match yields the whole prefix.
        "https://evil.automate-prod-recordings.s3.amazonaws.com/x.mp4",
    ],
)
async def test_aws_shaped_attacker_hosts_are_refused(
    monkeypatch: pytest.MonkeyPatch, hostile_url: str
) -> None:
    """AWS-owned-suffix hosts no longer satisfy the artifact allowlist.

    Every host here resolves to a PUBLIC address, so the private/CGNAT check
    cannot refuse them -- only the allowlist can. FAILS if ``amazonaws.com`` (or
    a bare ``cloudfront.net``) is put back into
    ``BROWSERSTACK_ARTIFACT_HOST_SUFFIXES``, or if the S3 bucket match degrades
    from an exact bucket label to a suffix match.
    """
    capture = _install(monkeypatch, video_url=hostile_url)

    assert await _farm().get_video("session-123") is None

    _assert_session_call_was_authenticated(capture)
    assert capture.artifact_requests == []
    _assert_no_credentials_left_the_vendor_api(capture)
    with pytest.raises(ArtifactUrlError, match="not allowlisted"):
        _validate_artifact_url(hostile_url)


@pytest.mark.parametrize(
    "legit_url",
    [
        "https://automate-prod-recordings.s3.amazonaws.com/session-123/video.mp4",
        "https://app-automate-prod-recordings.s3.eu-west-1.amazonaws.com/s/v.mp4",
    ],
)
def test_named_browserstack_s3_buckets_are_still_accepted(legit_url: str) -> None:
    """The narrowed allowlist still admits BrowserStack's own recording buckets.

    FAILS if ``BROWSERSTACK_ARTIFACT_S3_BUCKETS`` / ``_S3_VIRTUAL_HOST_RE`` are
    removed along with the wildcard, which would break every S3-hosted download.
    """
    assert _validate_artifact_url(legit_url) == _PUBLIC_IP


# ---------------------------------------------------------------------------
# Hole 2 -- unbounded artifact body
# ---------------------------------------------------------------------------


class _CountingStream:
    """Async byte stream that records how many chunks were actually pulled."""

    def __init__(self, chunk: bytes, chunks: int) -> None:
        self._chunk = chunk
        self._chunks = chunks
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.yielded += 1
            yield self._chunk


async def test_oversized_artifact_body_is_refused_without_buffering_it_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body larger than the cap aborts MID-STREAM, not after buffering it.

    FAILS if ``_download_artifact`` goes back to ``response.content`` (which
    materialises the whole body -- ``stream.yielded`` would then equal the full
    chunk count), or if the running-total ``ArtifactTooLargeError`` is removed.
    """
    monkeypatch.setattr("src.services.device_farm._ARTIFACT_MAX_BYTES", 1024)
    monkeypatch.setattr("src.services.device_farm._ARTIFACT_CHUNK_BYTES", 256)
    stream = _CountingStream(b"A" * 256, 4096)  # ~1MB if fully buffered

    capture = _install(
        monkeypatch,
        artifact_response=lambda _r: httpx.Response(200, content=stream),
    )

    assert await _farm().get_video("session-123") is None

    assert len(capture.artifact_requests) == 1
    # Cap is 1024 bytes over 256-byte chunks: a handful of pulls, not 4096.
    assert 0 < stream.yielded <= 16, f"stream was drained ({stream.yielded} chunks)"


async def test_oversized_content_length_is_refused_before_reading_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared ``content-length`` over the cap short-circuits the download.

    FAILS if the ``content-length`` pre-check is removed from
    ``_download_artifact`` (``stream.yielded`` would become non-zero).
    """
    monkeypatch.setattr("src.services.device_farm._ARTIFACT_MAX_BYTES", 1024)
    stream = _CountingStream(b"A" * 256, 4096)

    capture = _install(
        monkeypatch,
        artifact_response=lambda _r: httpx.Response(
            200, headers=[("content-length", "999999999")], content=stream
        ),
    )

    assert await _farm().get_video("session-123") is None

    assert len(capture.artifact_requests) == 1
    assert stream.yielded == 0, "body was read despite an oversized content-length"


def test_artifact_too_large_is_caught_by_get_videos_except_tuple() -> None:
    """``ArtifactTooLargeError`` must stay inside ``get_video``'s except tuple."""
    assert issubclass(ArtifactTooLargeError, ArtifactUrlError)


# ---------------------------------------------------------------------------
# Hole 3 -- a bare ValueError escaping the except tuple
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed_url",
    [
        "https://browserstack.com:99999999/v.mp4",  # "Port out of range 0-65535"
        "http://[::1/v.mp4",  # "Invalid IPv6 URL"
    ],
)
async def test_malformed_urls_return_none_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, malformed_url: str
) -> None:
    """Vendor-supplied junk must not propagate a bare ``ValueError`` out of ``get_video``.

    ``urlparse`` / ``.hostname`` / ``.port`` raise a PLAIN ``ValueError``, which
    is not in ``get_video``'s ``except`` tuple, so the exception escaped into the
    job path while every other artifact failure merely returns ``None``.
    FAILS if the ``except ValueError -> ArtifactUrlError`` conversion is removed
    from ``_validate_artifact_url`` (the test raises instead of returning None).
    """
    capture = _install(monkeypatch, video_url=malformed_url)

    assert await _farm().get_video("session-123") is None

    assert capture.artifact_requests == []
    with pytest.raises(ArtifactUrlError, match="Malformed artifact URL"):
        _validate_artifact_url(malformed_url)


# ---------------------------------------------------------------------------
# Hole 4 -- silent loss when the vendor artifact URL is not presigned
# ---------------------------------------------------------------------------


async def test_unauthorized_artifact_response_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A 401 on the credential-free artifact fetch must be OBSERVABLE.

    The credential-free client is the whole SSRF fix; if BrowserStack's
    ``video_url`` is not presigned it answers 401 and every session recording is
    lost. Returning ``None`` with no log line makes that permanent and
    undetectable. FAILS if the non-200 ``logger.warning`` is removed from
    ``_download_artifact``.
    """
    capture = _install(
        monkeypatch,
        artifact_response=lambda _r: httpx.Response(401, content=b"AccessDenied"),
    )

    with caplog.at_level(logging.WARNING, logger="src.services.device_farm"):
        assert await _farm().get_video("session-123") is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a failed artifact fetch produced no WARNING"
    message = warnings[0].getMessage()
    assert "401" in message
    assert _VIDEO_HOST in message
    # Host only: never the full (possibly presigned) URL, never credentials.
    assert "/s3/session-123/video.mp4" not in message
    assert "bs-access-key" not in message
    assert len(capture.artifact_requests) == 1
