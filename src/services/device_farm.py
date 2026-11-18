"""Device farm abstraction layer for provisioning and managing mobile devices."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import SecretStr

from src.api.constants import (
    DEVICE_FARM_ARTIFACT_CHUNK_BYTES,
    DEVICE_FARM_ARTIFACT_MAX_BYTES,
    DEVICE_FARM_BROWSERSTACK_CLIENT_TIMEOUT,
    DEVICE_FARM_BROWSERSTACK_HEALTH_TIMEOUT,
    DEVICE_FARM_BROWSERSTACK_UPLOAD_TIMEOUT,
    DEVICE_FARM_LOCAL_HEALTH_TIMEOUT,
)
from src.helpers.url_validation import SSRFError, pin_target, resolve_public_ip
from src.utils.redaction import redact_error_message

logger = logging.getLogger(__name__)


class ArtifactUrlError(ValueError):
    """Raised when a farm-supplied artifact URL fails the outbound guard."""


class ArtifactTooLargeError(ArtifactUrlError):
    """Raised when a farm-supplied artifact exceeds the download size cap."""


# Hosts BrowserStack serves session artifacts (videos, logs) from. The video URL
# is read out of the vendor's JSON *response body*, so it is attacker-controlled
# the moment that response is tampered with, spoofed, or the account is
# compromised. Resolving-to-a-public-IP is NOT sufficient on its own here:
# ``evil.example`` is public. The allowlist is what stops an arbitrary public
# host from being fetched at all.
#
# ⚠️ ``amazonaws.com`` used to be in this tuple "because BrowserStack serves
# session videos from S3". A bare AWS suffix is a wildcard that DEFEATS the
# allowlist: anybody with a free AWS account owns a passing hostname --
# ``evil-attacker-bucket.s3.amazonaws.com`` and
# ``ec2-1-2-3-4.compute-1.amazonaws.com`` both matched, so the only remaining
# protection was the private-IP check. It is removed. ``cloudfront.net`` is
# deliberately NOT added for exactly the same reason (any AWS account can mint a
# ``*.cloudfront.net`` distribution); a BrowserStack-owned CDN CNAME under
# ``browserstack.com`` is already covered by the suffix list below.
BROWSERSTACK_ARTIFACT_HOST_SUFFIXES = (
    "browserstack.com",
    "bsstag.com",
)

# S3 buckets BrowserStack publishes session recordings from. Matched as the
# EXACT bucket label of a virtual-hosted S3 endpoint, never as a suffix, so an
# attacker-owned bucket cannot satisfy it.
#
# If a legitimate recording download starts failing with "Artifact host not
# allowlisted", the fix is to add the observed bucket name HERE (the WARNING
# emitted on a refused/failed fetch names the host) -- never to re-broaden the
# suffix tuple back to ``amazonaws.com``.
BROWSERSTACK_ARTIFACT_S3_BUCKETS = frozenset(
    {
        "automate-prod-recordings",
        "app-automate-prod-recordings",
        "automate-sessions",
    }
)

# ``<bucket>.s3.amazonaws.com``, ``<bucket>.s3.<region>.amazonaws.com`` and the
# legacy ``<bucket>.s3-<region>.amazonaws.com`` / dualstack forms. The ``s3``
# label is mandatory, which is what excludes ``*.compute-1.amazonaws.com`` and
# every other AWS service endpoint. The bucket group is greedy and includes
# dots, so ``evil.automate-prod-recordings.s3.amazonaws.com`` yields bucket
# ``evil.automate-prod-recordings`` -- not in the set, refused.
_S3_VIRTUAL_HOST_RE = re.compile(
    r"^(?P<bucket>[a-z0-9][a-z0-9.\-]*)\.s3(?:[.-][a-z0-9\-]+)*\.amazonaws\.com$"
)

_ARTIFACT_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ARTIFACT_MAX_REDIRECTS = 3
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
# Session recordings are streamed to a cap rather than buffered whole -- see
# ``_download_artifact``. Mirrors ``app_acquisition._download_file``.
_ARTIFACT_MAX_BYTES = DEVICE_FARM_ARTIFACT_MAX_BYTES
_ARTIFACT_CHUNK_BYTES = DEVICE_FARM_ARTIFACT_CHUNK_BYTES


def _is_allowlisted_artifact_host(hostname: str) -> bool:
    """Return True if ``hostname`` is a vendor-owned artifact host."""
    if any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in BROWSERSTACK_ARTIFACT_HOST_SUFFIXES
    ):
        return True
    match = _S3_VIRTUAL_HOST_RE.match(hostname)
    return bool(match and match.group("bucket") in BROWSERSTACK_ARTIFACT_S3_BUCKETS)


def _parse_content_length(raw: str | None) -> int | None:
    """Parse a ``Content-Length`` header; ``None`` if absent or unparseable.

    A junk header must not raise out of the download path (the streaming cap
    still bounds the body), which is why this never lets ``int()``'s
    ``ValueError`` escape.
    """
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _validate_artifact_url(url: str) -> str:
    """Validate a farm-supplied artifact URL; return the public IP to pin to.

    Layered, all three required:

    1. scheme must be http(s) -- rejects ``file://``, ``gopher://``, ``data:``;
    2. no userinfo -- ``http://user:pass@browserstack.com@evil.example/`` parses  # pragma: allowlist secret
       to host ``evil.example`` (authority is whatever follows the LAST ``@``),
       and userinfo would additionally be replayed to that host by
       :func:`pin_target`. Artifact URLs never legitimately carry userinfo;
    3. host must match the vendor artifact allowlist;
    4. the host must then resolve to a public address --
       :func:`resolve_public_ip` rejects loopback/RFC1918/link-local/multicast
       **and CGNAT 100.64.0.0/10, which in this estate is the tailnet** -- and
       fails CLOSED on DNS failure. Its return value pins the connection, so the
       transport never re-resolves the name (DNS-rebind TOCTOU).

    Raises:
        ArtifactUrlError: scheme/userinfo/hostname/allowlist violation, or a URL
            so malformed that :func:`urllib.parse.urlparse` itself rejects it.
        SSRFError: host resolves to a blocked address, or DNS fails.
    """
    # ``urlparse`` and the ``.hostname``/``.port`` accessors raise a BARE
    # ``ValueError`` on vendor-supplied junk -- "Invalid IPv6 URL" for
    # ``http://[::1/v.mp4``, "Port out of range 0-65535" for
    # ``https://browserstack.com:99999999/v.mp4``. That is not in ``get_video``'s
    # except tuple, so it propagated out of the guard and into the job path while
    # every other artifact failure merely returns ``None``. Convert it here.
    # ``.port`` is touched (not used) so a bad port fails HERE rather than later
    # inside ``pin_target``, which is outside this function's try block.
    # NOTE: ``ArtifactUrlError`` IS a ``ValueError`` subclass -- the raises below
    # deliberately live OUTSIDE this try so they are not re-wrapped.
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        userinfo = bool(parsed.username or parsed.password)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        _ = parsed.port
    except ValueError as exc:
        raise ArtifactUrlError(f"Malformed artifact URL ({exc})") from exc

    if scheme not in _ARTIFACT_ALLOWED_SCHEMES:
        raise ArtifactUrlError(f"Artifact URL scheme not allowed: {scheme!r}")
    if userinfo:
        raise ArtifactUrlError("Artifact URL must not contain userinfo credentials")
    if not hostname:
        raise ArtifactUrlError(f"Cannot extract hostname from artifact URL: {url}")
    if not _is_allowlisted_artifact_host(hostname):
        raise ArtifactUrlError(f"Artifact host not allowlisted: {hostname}")
    return resolve_public_ip(url)


@dataclass
class DeviceSpec:
    """Specification for a target device."""

    name: str = "default"
    os_version: str = ""
    platform: str = "android"
    real_device: bool = False


@dataclass
class SessionInfo:
    """Information about an active device session."""

    session_id: str
    appium_url: str
    capabilities: dict
    device: DeviceSpec
    app_url: str = ""


class DeviceFarmClient(ABC):
    """Abstract interface for device farm providers."""

    @abstractmethod
    async def upload_app(self, app_path: str) -> str:
        """Upload app binary. Returns farm-specific app URL."""

    @abstractmethod
    async def get_appium_url(self) -> str:
        """Get the Appium hub URL."""

    @abstractmethod
    async def get_capabilities(self, device: DeviceSpec | None = None) -> dict:
        """Get extra capabilities for session creation."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Verify farm connectivity."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name."""

    async def get_video(self, session_id: str = "") -> bytes | None:
        """Download session recording. Returns None if not supported."""
        return None

    async def end_session(self, session_id: str = "") -> None:
        """Clean up device session."""
        return

    async def list_devices(self) -> list[dict]:
        """List available devices. Returns empty list if not supported."""
        return []


class LocalFarm(DeviceFarmClient):
    """Local Appium server -- passthrough that uses a local Appium instance."""

    def __init__(self, appium_url: str = "http://localhost:4723") -> None:
        self._appium_url = appium_url

    @property
    def name(self) -> str:
        return "local"

    async def upload_app(self, app_path: str) -> str:
        """Return the path unchanged since no upload is needed locally."""
        return app_path

    async def get_appium_url(self) -> str:
        """Return the configured local Appium URL."""
        return self._appium_url

    async def get_capabilities(self, device: DeviceSpec | None = None) -> dict:
        """Return default UiAutomator2 capabilities."""
        caps: dict = {
            "platformName": (device.platform.capitalize() if device else "Android"),
            "appium:automationName": "UiAutomator2",
            "appium:noReset": True,
        }
        if device:
            if device.name and device.name != "default":
                caps["appium:deviceName"] = device.name
            if device.os_version:
                caps["appium:platformVersion"] = device.os_version
        return caps

    async def health_check(self) -> bool:
        """Check local Appium server status."""
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=DEVICE_FARM_LOCAL_HEALTH_TIMEOUT) as client:
                response = await client.get(f"{self._appium_url}/status")
                ok = response.status_code == 200
                logger.debug("Local Appium health check: %s (%.3fs)", ok, time.monotonic() - t0)
                return ok
        except httpx.HTTPError, OSError:
            logger.warning("Local Appium health check failed (%.3fs)", time.monotonic() - t0)
            return False


class DockerAndroidFarm(DeviceFarmClient):
    """Docker-based Android emulator farm using a named container."""

    # Every ``docker`` invocation is bounded. A wedged docker CLI used to hang
    # the health check and /api/farms forever.
    _subprocess_timeout: float = 30.0

    def __init__(self, container_name: str = "android-emulator") -> None:
        self._container_name = container_name
        self._appium_url = "http://localhost:4723"

    async def _run_docker(self, *args: str) -> tuple[int, bytes, bytes]:
        """Run a ``docker`` command, DRAINING its pipes, bounded by a timeout.

        ``await proc.wait()`` on a process spawned with ``stdout=PIPE`` is a
        deadlock waiting to happen: past the ~64KiB pipe buffer the child blocks
        writing while we block waiting, forever and with no timeout.
        ``communicate()`` drains both pipes, and ``wait_for`` bounds the whole
        thing so a hung docker daemon degrades instead of hanging the caller.

        Returns:
            ``(returncode, stdout, stderr)``. A timeout is reported as a
            non-zero return code after killing the child, never as success.
        """
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._subprocess_timeout
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            logger.warning(
                "docker %s timed out after %.1fs",
                args[0] if args else "?",
                self._subprocess_timeout,
            )
            return 1, b"", b"docker command timed out"
        return proc.returncode or 0, stdout or b"", stderr or b""

    @property
    def name(self) -> str:
        return "docker_android"

    async def upload_app(self, app_path: str) -> str:
        """Copy the app binary into the Docker container.

        Raises:
            ValueError: If the app is an iOS binary.
        """
        if app_path.lower().endswith((".ipa", ".app")):
            raise ValueError("DockerAndroidFarm does not support iOS apps")

        returncode, _stdout, stderr = await self._run_docker(
            "cp", app_path, f"{self._container_name}:/tmp/app.apk"
        )
        if returncode != 0:
            # CRITICAL: returning the path anyway left whatever APK the SHARED
            # container already held in place, so Appium would inspect the
            # PREVIOUS job's app — potentially another tenant's binary — and
            # report the result as this job's.
            detail = stderr.decode("utf-8", "replace").strip() or f"exit code {returncode}"
            raise RuntimeError(
                f"docker cp failed to upload the app into container "
                f"'{self._container_name}': {detail}"
            )
        return "/tmp/app.apk"  # noqa: S108  # nosec B108 -- fixed container-internal path for Docker emulator

    async def get_appium_url(self) -> str:
        """Return the container's Appium hub URL."""
        return self._appium_url

    async def get_capabilities(self, device: DeviceSpec | None = None) -> dict:
        """Return Android-only capabilities for the Docker emulator."""
        return {"platformName": "Android"}

    async def health_check(self) -> bool:
        """Check that the Docker container is running."""
        try:
            returncode, _stdout, _stderr = await self._run_docker("inspect", self._container_name)
            return returncode == 0
        except httpx.HTTPError, OSError:
            return False

    async def list_devices(self) -> list[dict]:
        """List Docker Android containers matching this farm's container name."""
        import asyncio
        import json

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "ps",
                "--filter",
                f"name={self._container_name}",
                "--format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0 or not stdout.strip():
                return []
            devices = []
            for line in stdout.decode().strip().splitlines():
                line = line.strip()
                if line:
                    devices.append(json.loads(line))
            return devices
        except httpx.HTTPError, OSError:
            logger.warning("Failed to list Docker Android devices")
            return []

    async def get_video(self, session_id: str = "") -> bytes | None:
        """Copy recording from the Docker container."""
        import asyncio

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "cp",
                f"{self._container_name}:/tmp/recording.mp4",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0 or not stdout:
                return None
            return stdout
        except httpx.HTTPError, OSError:
            logger.warning("Failed to get video from Docker container")
            return None


class BrowserStackFarm(DeviceFarmClient):
    """BrowserStack App Automate cloud farm.

    Attributes:
        _username: BrowserStack username.
        _access_key: BrowserStack access key.
    """

    def __init__(self, username: str, access_key: str) -> None:
        self._username = username
        self._access_key = access_key

    @property
    def name(self) -> str:
        return "browserstack"

    async def upload_app(self, app_path: str) -> str:
        """Upload app binary to BrowserStack and return the app URL."""
        logger.info("Uploading app to BrowserStack: %s", app_path)
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=DEVICE_FARM_BROWSERSTACK_UPLOAD_TIMEOUT) as client:
            with Path(app_path).open("rb") as f:  # noqa: ASYNC230 — why: file handle for httpx multipart upload; sync open() returns a handle, not a blocking-IO op
                response = await client.post(
                    "https://api-cloud.browserstack.com/app-automate/upload",
                    auth=(self._username, self._access_key),
                    files={"file": f},
                )
            response.raise_for_status()
            app_url = response.json().get("app_url", "")
            logger.info(
                "BrowserStack upload completed in %.1fs: %s", time.monotonic() - t0, app_url
            )
            return app_url

    async def get_appium_url(self) -> str:
        """Return the BrowserStack Appium hub URL with embedded credentials."""
        return f"https://{self._username}:{self._access_key}@hub-cloud.browserstack.com/wd/hub"

    async def get_capabilities(self, device: DeviceSpec | None = None) -> dict:
        """Return BrowserStack-specific capabilities."""
        caps: dict = {
            "bstack:options": {
                "video": True,
                "debug": True,
                "appiumVersion": "2.0.0",
            },
        }
        if device:
            caps["bstack:options"]["deviceName"] = device.name
            caps["bstack:options"]["osVersion"] = device.os_version
            caps["bstack:options"]["realMobile"] = device.real_device
        return caps

    async def health_check(self) -> bool:
        """Check BrowserStack API connectivity."""
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=DEVICE_FARM_BROWSERSTACK_HEALTH_TIMEOUT) as client:
                response = await client.get(
                    "https://api-cloud.browserstack.com/app-automate/plan.json",
                    auth=(self._username, self._access_key),
                )
                ok = response.status_code == 200
                logger.debug("BrowserStack health check: %s (%.3fs)", ok, time.monotonic() - t0)
                return ok
        except httpx.HTTPError, OSError:
            logger.warning("BrowserStack health check failed (%.3fs)", time.monotonic() - t0)
            return False

    async def _download_artifact(self, url: str) -> bytes | None:
        """Fetch a farm-supplied artifact URL on a CREDENTIAL-FREE client.

        The BrowserStack credentials are deliberately NOT available here: this
        method builds its own ``AsyncClient`` with no ``auth=``, so no
        ``Authorization`` header can be generated for the artifact host no
        matter what URL the vendor response body contained. Reusing the
        authenticated client (the previous behaviour) sent HTTP Basic creds to
        whatever host ``video_url`` named.

        Redirects are followed MANUALLY (``follow_redirects=False``) so every
        hop is re-run through :func:`_validate_artifact_url` -- httpx's own
        redirect following would only ever have validated the first URL, and a
        302 off an allowlisted host is the obvious bypass.

        The body is STREAMED against a byte cap rather than buffered whole
        (``response.content`` would materialise whatever the vendor sent -- a
        50MB body was demonstrated to buffer completely, and nothing bounds a
        response body we do not control). This mirrors the sibling downloader
        :meth:`src.services.app_acquisition.AppAcquisitionService._download_file`
        exactly: ``client.stream`` + a ``content-length`` pre-check + a running
        total that aborts mid-stream.

        Raises:
            ArtifactUrlError: URL (or a redirect hop) failed the guard.
            ArtifactTooLargeError: declared or actual body exceeded the cap.
            SSRFError: URL (or a redirect hop) resolved to a blocked address.
        """
        current_url = url
        current_ip = _validate_artifact_url(current_url)
        async with httpx.AsyncClient(
            timeout=DEVICE_FARM_BROWSERSTACK_CLIENT_TIMEOUT,
            follow_redirects=False,
        ) as client:
            for _hop in range(_ARTIFACT_MAX_REDIRECTS + 1):
                request_url, pin_headers, pin_ext = pin_target(current_url, current_ip)
                async with client.stream(
                    "GET", request_url, headers=pin_headers, extensions=pin_ext
                ) as response:
                    if response.status_code in _REDIRECT_STATUS:
                        location = response.headers.get("location")
                        if not location:
                            raise ArtifactUrlError(f"Redirect with no Location from {current_url}")
                        # Re-validate + re-pin from the hop's OWN hostname.
                        current_url = urljoin(current_url, location)
                        current_ip = _validate_artifact_url(current_url)
                        continue
                    if response.status_code != 200:
                        # Observability for the silent-failure mode our own
                        # credential-free artifact client introduces: if the
                        # vendor's ``video_url`` is NOT presigned, this client
                        # gets 401/403 and EVERY session video is lost. Without
                        # this line that loss is permanent and undetectable.
                        # Host only -- never the full URL (it may carry a
                        # presigned signature) and never credentials.
                        logger.warning(
                            "BrowserStack artifact fetch returned HTTP %s from host %s "
                            "(hop %d) -- session recording will be missing. A 401/403 "
                            "here means the vendor artifact URL is not presigned and "
                            "the credential-free artifact client cannot read it.",
                            response.status_code,
                            pin_ext.get("sni_hostname", "<unknown>"),
                            _hop,
                        )
                        return None

                    declared = _parse_content_length(response.headers.get("content-length"))
                    if declared is not None and declared > _ARTIFACT_MAX_BYTES:
                        raise ArtifactTooLargeError(
                            f"Artifact too large: {declared} bytes "
                            f"(max {_ARTIFACT_MAX_BYTES} bytes)"
                        )
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=_ARTIFACT_CHUNK_BYTES):
                        buffer += chunk
                        if len(buffer) > _ARTIFACT_MAX_BYTES:
                            raise ArtifactTooLargeError(
                                f"Artifact download exceeded max size: {_ARTIFACT_MAX_BYTES} bytes"
                            )
                    return bytes(buffer)
            raise ArtifactUrlError(f"Too many redirects (> {_ARTIFACT_MAX_REDIRECTS}) for {url}")

    async def get_video(self, session_id: str = "") -> bytes | None:
        """Download session recording from BrowserStack."""
        if not session_id:
            return None
        video_url: str = ""
        try:
            async with httpx.AsyncClient(
                auth=(self._username, self._access_key),
                timeout=DEVICE_FARM_BROWSERSTACK_CLIENT_TIMEOUT,
            ) as client:
                response = await client.get(
                    f"https://api-cloud.browserstack.com/app-automate/sessions/{session_id}.json",
                )
                if response.status_code != 200:
                    return None
                data = response.json()
                video_url = data.get("automation_session", {}).get("video_url")
            # The credentialed client is closed before the artifact is fetched:
            # the separation is structural, not a convention a later edit can
            # quietly undo by adding one more `await client.get(...)`.
            if not video_url:
                return None
            return await self._download_artifact(video_url)
        except httpx.HTTPError, OSError, ArtifactUrlError, SSRFError:
            logger.warning("Failed to get video from BrowserStack for session %s", session_id)
            return None

    async def list_devices(self) -> list[dict]:
        """List available devices from BrowserStack."""
        try:
            async with httpx.AsyncClient(
                auth=(self._username, self._access_key),
                timeout=DEVICE_FARM_BROWSERSTACK_CLIENT_TIMEOUT,
            ) as client:
                response = await client.get(
                    "https://api-cloud.browserstack.com/app-automate/devices.json",
                )
                if response.status_code != 200:
                    return []
                return response.json()
        except httpx.HTTPError, OSError:
            logger.warning("Failed to list BrowserStack devices")
            return []

    async def end_session(self, session_id: str = "") -> None:
        """Mark a BrowserStack session as completed."""
        if not session_id:
            return
        try:
            async with httpx.AsyncClient(
                auth=(self._username, self._access_key),
                timeout=DEVICE_FARM_BROWSERSTACK_CLIENT_TIMEOUT,
            ) as client:
                response = await client.put(
                    f"https://api-cloud.browserstack.com/app-automate/sessions/{session_id}.json",
                    json={"status": "completed"},
                )
                response.raise_for_status()
        except httpx.HTTPError, OSError:
            logger.warning("Failed to end BrowserStack session %s", session_id)


class DeviceFarmCredentialsUnavailableError(RuntimeError):
    """A job needs per-request farm credentials that are not in memory.

    Credentials are carried in memory only (never persisted), so a job whose
    background task no longer holds them cannot run -- the caller must resubmit.
    """


@dataclass(frozen=True, slots=True)
class DeviceFarmCredentials:
    """Per-request device farm credentials, held in memory only.

    Never persisted to ``inspection_jobs`` and never logged: ``access_key`` is a
    ``SecretStr`` and excluded from ``repr``.
    """

    username: str
    access_key: SecretStr = field(repr=False)


def redact_farm_error(text: str, credentials: DeviceFarmCredentials | None) -> str:
    """Scrub ``text`` of this job's farm credentials (every encoding) + generic secrets.

    Use on any failure message that is persisted to ``jobs.error_message`` or
    logged: a farm/Appium/httpx error can quote the access key in prose, in a
    hub URL, or as an HTTP Basic header -- see :func:`redact_error_message`.
    """
    if credentials is None:
        return redact_error_message(text)
    return redact_error_message(
        text,
        username=credentials.username,
        access_key=credentials.access_key.get_secret_value(),
    )


# Providers whose client cannot be constructed without per-request credentials.
CREDENTIALED_PROVIDERS = frozenset({"browserstack"})


def device_farm_kwargs(provider: str, credentials: DeviceFarmCredentials | None) -> dict[str, str]:
    """Return constructor kwargs for ``provider`` from in-memory credentials.

    Raises:
        DeviceFarmCredentialsUnavailableError: ``provider`` needs credentials
            and none are held for this job.
    """
    if provider not in CREDENTIALED_PROVIDERS:
        return {}
    if credentials is None:
        raise DeviceFarmCredentialsUnavailableError(
            f"device farm credentials unavailable for provider '{provider}' "
            "(they are held in memory only and are not persisted) -- resubmit the "
            "inspection with browserstack_username and browserstack_access_key"
        )
    return {
        "username": credentials.username,
        "access_key": credentials.access_key.get_secret_value(),
    }


# Available farm providers
FARM_PROVIDERS = {
    "local": LocalFarm,
    "docker_android": DockerAndroidFarm,
    "browserstack": BrowserStackFarm,
}


def get_device_farm(provider: str, **kwargs: Any) -> DeviceFarmClient:
    """Factory to get a device farm client by provider name.

    Args:
        provider: Provider key (local, docker_android, browserstack).
        **kwargs: Extra keyword arguments forwarded to the provider constructor.

    Returns:
        An instance of the requested DeviceFarmClient subclass.

    Raises:
        ValueError: If the provider name is not registered.
    """
    cls = FARM_PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown device farm: {provider}. Available: {sorted(FARM_PROVIDERS.keys())}"
        )
    return cls(**kwargs)
