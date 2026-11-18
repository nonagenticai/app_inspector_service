"""App acquisition service -- resolves app inputs to local file paths.

Handles Play Store URLs, HTTP(S) URLs, MinIO references, and local paths.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import tempfile
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote, urljoin, urlparse

import httpx

from src.config import Settings, get_settings
from src.helpers.url_validation import (
    SSRFError,
    pin_target,
    resolve_public_ip,
    validate_url_safe,
)
from src.utils.redaction import redact_text, redact_url_credentials

logger = logging.getLogger(__name__)

# Manual redirect handling so every hop is re-checked against the SSRF
# allowlist (see ``_download_file``). httpx's own ``follow_redirects`` would
# only validate the original URL.
_MAX_REDIRECTS = 5
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


@runtime_checkable
class MinioLike(Protocol):
    """Duck-typed MinIO storage with download_file method."""

    def download_file(self, bucket: str, key: str, dest: str) -> Coroutine[Any, Any, None]: ...


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_PLAY_STORE_RE = re.compile(
    r"https?://play\.google\.com/store/apps/details\?.*?id=([\w.]+)",
    re.IGNORECASE,
)

_APP_STORE_RE = re.compile(
    r"https?://(?:apps\.apple\.com|itunes\.apple\.com)/",
    re.IGNORECASE,
)

# Any ``scheme://`` prefix. Used to reject schemes we do not explicitly support
# (``s3://``, ``file://``, ``gs://``, …) instead of letting them fall through to
# the local-path branch, where ``Path("s3://x/y")`` silently becomes a relative
# path under the CWD.
URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

# Schemes the service accepts on ``app_path``.
SUPPORTED_URI_SCHEMES = ("http://", "https://", "minio://")

# Valid app file extensions
_VALID_EXTENSIONS = frozenset({".apk", ".aab", ".ipa", ".app", ".xapk"})

# APK/IPA/ZIP magic bytes  (PK header = 0x504B0304)
_PK_MAGIC = b"PK"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AppAcquisitionError(Exception):
    """Base error for app acquisition failures."""


class AppStoreDownloadError(AppAcquisitionError):
    """Raised when an iOS App Store URL is provided (not supported)."""


class DownloadError(AppAcquisitionError):
    """Raised when downloading an app from a URL fails."""


class ValidationError(AppAcquisitionError):
    """Raised when the downloaded/resolved file fails validation."""


class ConfinementError(AppAcquisitionError):
    """Raised when a storage URI escapes the allowed bucket / tenant prefix."""


# ---------------------------------------------------------------------------
# Storage confinement
# ---------------------------------------------------------------------------
#
# ``w2v_common.storage.StorageService`` has ZERO tenant concept: whatever
# bucket + key it is handed, it fetches. A ``minio://<bucket>/<key>`` app_path
# is caller-controlled, so a wired-up MinIO backend would let tenant A name any
# object in any bucket the service's MinIO credentials can read. Confinement is
# therefore this caller's job and is enforced in exactly one place, used by
# both the API route (fail fast, 400) and the acquisition service (fail
# closed, defense in depth).
#
# SCOPE — read before believing this closed a live leak. It did NOT.
#   * ``InspectorService`` builds ``AppAcquisitionService()`` with no
#     ``minio_storage`` (see ``_default_app_acquirer_factory``), and
#     ``__init__`` defaults ``minio_storage=None``. Nothing in ``src/`` ever
#     passes one. So in production every ``minio://`` app_path ALREADY died in
#     the background job with ``AppAcquisitionError("MinIO storage is not
#     configured…")``. This module is DEFENCE-IN-DEPTH ON A CURRENTLY
#     NON-FUNCTIONAL INPUT SCHEME, pre-positioned for the day MinIO is wired
#     up — not the closing of an exploitable cross-tenant read.
#   * The one real behaviour change shipped: a bad ``app_path`` now fails at
#     submit time with HTTP 400 instead of returning 202 and failing later
#     inside the job.
#
# ACCEPT-SET WARNING — the tenant-prefix rule currently accepts NOTHING.
#   ``resolve_allowed_minio_bucket`` falls back to ``STORAGE_MINIO_BUCKET``
#   (``app-inspector-service``), and this service writes its own objects as
#   ``f"{job_id}/recording.mp4"`` (``storage_service.store_recording``) — no
#   object it stores carries a ``<tenant_id>/`` prefix, and no gitops value for
#   ``ACQUISITION_ALLOWED_MINIO_BUCKET`` is shipped. The rule is still the
#   CORRECT one: ``app_path`` is a CALLER-SUPPLIED INPUT ref, and
#   ``{job_id}/…`` is this service's OUTPUT key shape, not an input shape.
#   But it means wiring MinIO acquisition up requires a producer that writes
#   uploads under ``<tenant_id>/`` in the allowed bucket. Until such a producer
#   exists, every ``minio://`` app_path is rejected — by design, stated here so
#   nobody debugs it as a mystery 400.


def _fully_unquote(value: str, *, max_passes: int = 4) -> str:
    """Percent-decode until stable, so ``%2e%2e`` / ``%252f`` cannot hide a ``..``."""
    for _ in range(max_passes):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def _is_confined(key: str, prefix: str) -> bool:
    """True when ``key`` names an object strictly beneath ``prefix``.

    Compares path SEGMENTS after normalisation — a string ``startswith`` would
    let ``<tenant>evil/app.apk`` pass a ``<tenant>`` test.
    """
    normalized = posixpath.normpath(key)
    base = posixpath.normpath(prefix)
    return normalized.startswith(base + "/")


def resolve_allowed_minio_bucket(settings: Settings) -> str:
    """The single bucket a ``minio://`` app_path may name (``""`` when unset).

    ``ACQUISITION_ALLOWED_MINIO_BUCKET`` wins; otherwise the service's own
    storage bucket. An empty result is fail-closed: every ``minio://`` input is
    rejected.
    """
    configured = settings.acquisition.allowed_minio_bucket
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    fallback = settings.storage.minio_bucket
    # ``isinstance`` keeps a mis-typed/mocked settings object fail-closed rather
    # than letting a non-string sail into the bucket comparison.
    return fallback.strip() if isinstance(fallback, str) else ""


def confine_minio_uri(uri: str, *, tenant_id: str | None, allowed_bucket: str) -> tuple[str, str]:
    """Validate a ``minio://bucket/key`` URI against bucket + tenant confinement.

    Args:
        uri: The caller-supplied ``minio://`` URI.
        tenant_id: The requesting tenant. Required — objects must live under
            ``<tenant_id>/`` as a path SEGMENT.
        allowed_bucket: The one bucket that may be named.

    Returns:
        ``(bucket, key)`` — the RAW (still percent-encoded) key, path-normalised
        only, safe to hand to ``StorageService.download_file``.

        Raw vs decoded is load-bearing: ``%`` is a legal character in an S3/MinIO
        object key, so ``<tenant>/My%20App.apk`` and ``<tenant>/My App.apk`` are
        two DIFFERENT objects. Decoding is used to DECIDE SAFETY (a ``%2e%2e``
        must not hide a traversal); it must never rewrite the identifier we then
        fetch, or a legitimate object gets silently swapped for another one — or
        404s. Hence: confine on the decoded form, return the raw form.

    Raises:
        ConfinementError: On any other bucket, a missing/unsafe tenant, a key
            outside the tenant prefix, or a traversal attempt (plain,
            percent-encoded, or double-encoded).
    """
    if not allowed_bucket:
        raise ConfinementError(
            "MinIO acquisition is not configured (no allowed bucket); "
            "minio:// app_path values are rejected."
        )
    if not tenant_id or not tenant_id.strip():
        raise ConfinementError("minio:// app_path requires an authenticated tenant.")
    tenant = tenant_id.strip()
    if "/" in tenant or ".." in tenant or tenant != _fully_unquote(tenant):
        raise ConfinementError(f"Unsafe tenant identifier for storage confinement: {tenant!r}")

    parsed = urlparse(uri)
    if parsed.scheme != "minio":
        raise ConfinementError(f"Expected a minio:// URI, got scheme {parsed.scheme!r}")
    bucket = parsed.netloc
    raw_key = parsed.path.lstrip("/")
    if not bucket or not raw_key:
        raise ConfinementError(f"Invalid MinIO URI: {uri}")

    if bucket != allowed_bucket:
        raise ConfinementError(
            f"MinIO bucket '{bucket}' is not permitted; only '{allowed_bucket}' may be read."
        )

    decoded_key = _fully_unquote(raw_key).lstrip("/")
    # Both forms must be confined: the raw key is what MinIO stores under, the
    # decoded key is what a proxy/SDK may collapse it to.
    if not (_is_confined(raw_key, tenant) and _is_confined(decoded_key, tenant)):
        raise ConfinementError(
            f"MinIO object key must live under the tenant prefix '{tenant}/'; "
            f"refusing to access '{raw_key}'."
        )

    # Return the RAW key, normalised only (``./`` collapsed). Returning
    # ``decoded_key`` here would rewrite ``<tenant>/My%20App.apk`` into
    # ``<tenant>/My App.apk`` and fetch a different object. See the Returns
    # section above.
    return bucket, posixpath.normpath(raw_key)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AcquiredApp:
    """Result of acquiring an app from any supported input."""

    local_path: Path
    platform: str
    original_input: str
    farm_app_url: str = field(default="")
    # True only when acquisition produced a tempfile it OWNS (mkstemp output
    # from _download_file / _from_minio). False for a user-supplied local path
    # (_from_local_path returns the ORIGINAL source file) — deleting that on
    # success is data loss and breaks retry/resume + concurrent jobs. Callers
    # must gate any unlink on this flag.
    is_temp: bool = field(default=False)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AppAcquisitionService:
    """Resolves app inputs (URLs, store links, MinIO refs, local paths) to local files."""

    def __init__(self, *, minio_storage: MinioLike | None = None) -> None:
        # ``minio_storage`` defaults to None and NO caller in ``src/`` supplies
        # one, so the minio:// branch is unreachable in production today (it
        # raises "MinIO storage is not configured"). Tests inject a fake.
        self._minio = minio_storage
        settings = get_settings()
        self._download_timeout = settings.acquisition.download_timeout
        self._max_file_size = settings.acquisition.max_file_size_mb * 1024 * 1024
        self._temp_dir = Path(settings.acquisition.temp_dir)
        self._allowed_local_paths = settings.acquisition.allowed_local_paths
        self._allowed_minio_bucket = resolve_allowed_minio_bucket(settings)

    async def acquire(self, input_str: str, /, *, tenant_id: str | None = None) -> AcquiredApp:
        """Acquire an app from any supported input type.

        Dispatch order:
        1. Play Store URL
        2. App Store URL (rejected)
        3. minio:// URI (bucket + tenant-prefix confined)
        4. HTTP(S) URL
        5. Unsupported scheme (rejected)
        6. Local file path

        Args:
            input_str: The caller-supplied app location.
            tenant_id: Requesting tenant. Required for ``minio://`` inputs —
                without it the object cannot be confined and is rejected.

        Returns:
            AcquiredApp with resolved local_path and detected platform.

        Raises:
            AppAcquisitionError: On any acquisition failure.
            ConfinementError: When a ``minio://`` URI names another bucket or
                escapes the tenant's key prefix.
        """
        input_str = input_str.strip()
        if not input_str:
            raise AppAcquisitionError("App input cannot be empty")

        # Play Store URL
        match = _PLAY_STORE_RE.match(input_str)
        if match:
            return await self._from_play_store(input_str, match.group(1))

        # App Store URL
        if _APP_STORE_RE.match(input_str):
            raise AppStoreDownloadError(
                "iOS App Store downloads are not supported programmatically. "
                "Please upload the .ipa file directly via the web UI or CLI."
            )

        # MinIO reference
        if input_str.startswith("minio://"):
            return await self._from_minio(input_str, tenant_id=tenant_id)

        # HTTP(S) URL
        if input_str.startswith(("http://", "https://")):
            return await self._from_url(input_str)

        # Any other scheme (s3://, file://, gs://, …) must NOT fall through to
        # the local-path branch, where it would be re-read as a relative path.
        if URI_SCHEME_RE.match(input_str):
            raise AppAcquisitionError(
                f"Unsupported URI scheme in app input: {input_str!r}. "
                f"Supported: {', '.join(SUPPORTED_URI_SCHEMES)} or a local path."
            )

        # Local file path
        return self._from_local_path(input_str)

    # ------------------------------------------------------------------
    # Private acquisition methods
    # ------------------------------------------------------------------

    async def _from_play_store(self, url: str, package_id: str) -> AcquiredApp:
        """Download APK via a Play Store URL.

        Uses a public APK mirror/download service as a fallback since the
        Play Store itself does not offer a direct download API.
        """
        logger.info("Play Store acquisition for package: %s", package_id)

        # Use a well-known APK download proxy endpoint
        download_url = f"https://d.apkpure.com/b/APK/{package_id}?version=latest"
        validate_url_safe(download_url)

        local_path = await self._download_file(download_url, suffix=".apk")
        self._validate_magic_bytes(local_path)

        return AcquiredApp(
            local_path=local_path,
            platform="android",
            original_input=url,
            is_temp=True,
        )

    async def _from_minio(self, uri: str, *, tenant_id: str | None = None) -> AcquiredApp:
        """Download app from a minio:// URI, confined to one bucket + tenant prefix.

        The API route already rejects escapes with a 400; this is the
        fail-closed second gate for any other caller of the service.
        """
        if self._minio is None:
            raise AppAcquisitionError(
                "MinIO storage is not configured but a minio:// URI was provided"
            )

        # Parse + confine minio://bucket/<tenant_id>/path/to/file.apk
        bucket, object_key = confine_minio_uri(
            uri, tenant_id=tenant_id, allowed_bucket=self._allowed_minio_bucket
        )

        ext = Path(object_key).suffix.lower()
        if ext not in _VALID_EXTENSIONS:
            raise ValidationError(
                f"Unsupported file extension '{ext}' in MinIO object. "
                f"Expected one of: {', '.join(sorted(_VALID_EXTENSIONS))}"
            )

        platform = _detect_platform_from_ext(ext)

        self._temp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=str(self._temp_dir))
        os.close(fd)
        local_path = Path(tmp_path)

        logger.info("Downloading from MinIO: %s/%s", bucket, object_key)

        try:
            await self._minio.download_file(bucket, object_key, str(local_path))
        except Exception as exc:
            raise DownloadError(f"Failed to download from MinIO: {exc}") from exc

        self._validate_magic_bytes(local_path)

        return AcquiredApp(
            local_path=local_path,
            platform=platform,
            original_input=uri,
            is_temp=True,
        )

    async def _from_url(self, url: str) -> AcquiredApp:
        """Download app from an HTTP(S) URL with SSRF protection."""
        validate_url_safe(url)

        # Determine extension from URL path
        parsed = urlparse(url)
        url_path = Path(parsed.path)
        ext = url_path.suffix.lower() if url_path.suffix else ".apk"

        if ext not in _VALID_EXTENSIONS:
            raise ValidationError(
                f"Unsupported file extension '{ext}' from URL. "
                f"Expected one of: {', '.join(sorted(_VALID_EXTENSIONS))}"
            )

        platform = _detect_platform_from_ext(ext)
        local_path = await self._download_file(url, suffix=ext)
        self._validate_magic_bytes(local_path)

        return AcquiredApp(
            local_path=local_path,
            platform=platform,
            original_input=url,
            is_temp=True,
        )

    def _from_local_path(self, path_str: str) -> AcquiredApp:
        """Resolve a local file path with path traversal protection."""
        local_path = Path(path_str).resolve()  # Canonicalize (resolves ../)

        # Enforce allowed base directories
        allowed_bases = [Path(p).resolve() for p in self._allowed_local_paths]
        if not any(local_path.is_relative_to(base) for base in allowed_bases):
            raise AppAcquisitionError(f"Path not within allowed directories: {path_str}")

        if not local_path.exists():
            raise AppAcquisitionError(f"Local file not found: {path_str}")

        ext = local_path.suffix.lower()
        if ext not in _VALID_EXTENSIONS:
            raise ValidationError(
                f"Unsupported file extension '{ext}'. "
                f"Expected one of: {', '.join(sorted(_VALID_EXTENSIONS))}"
            )

        platform = _detect_platform_from_ext(ext)

        return AcquiredApp(
            local_path=local_path,
            platform=platform,
            original_input=path_str,
            # This is the USER'S original file, not a tempfile we own — the
            # caller must NOT delete it after inspection (data loss + breaks
            # retry/resume and concurrent jobs sharing the input).
            is_temp=False,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _download_file(self, url: str, *, suffix: str = ".apk") -> Path:
        """Download a file from a URL to a temporary location."""
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=str(self._temp_dir))
        os.close(fd)
        dest = Path(tmp_path)

        logger.info("Downloading %s -> %s", redact_url_credentials(url), dest)

        # Redirects are followed manually so each hop is re-checked (a public
        # host can 30x to an internal address — link-local metadata, RFC1918,
        # loopback). Each hop's connection is PINNED to the IP the SSRF guard
        # resolved (host rewritten to the literal IP, Host header + TLS SNI kept
        # as the original hostname): the validator and httpx would otherwise
        # resolve the name independently, leaving a DNS-rebind TOCTOU window
        # where the guard sees a public IP and httpx connects to a flipped
        # internal one. Mirrors w2v_common.net.safe_fetch's pinning.
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._download_timeout),
                follow_redirects=False,
            ) as client:
                current_url = url
                current_ip = resolve_public_ip(url)  # SSRF check + pin target
                for _hop in range(_MAX_REDIRECTS + 1):
                    request_url, pin_headers, pin_ext = pin_target(current_url, current_ip)
                    async with client.stream(
                        "GET", request_url, headers=pin_headers, extensions=pin_ext
                    ) as response:
                        if response.status_code in _REDIRECT_STATUS:
                            location = response.headers.get("location")
                            if not location:
                                raise DownloadError(
                                    f"Redirect with no Location header from "
                                    f"{redact_url_credentials(current_url)}"
                                )
                            current_url = urljoin(current_url, location)
                            # SSRF re-check + re-pin from the redirect target's
                            # OWN hostname on every hop.
                            current_ip = resolve_public_ip(current_url)
                            continue

                        response.raise_for_status()

                        # Check content-length header if available
                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > self._max_file_size:
                            raise DownloadError(
                                f"File too large: {int(content_length)} bytes "
                                f"(max {self._max_file_size} bytes)"
                            )

                        total = 0
                        with dest.open("wb") as f:  # noqa: ASYNC230 — why: download body streams to disk via async iter; sync open() here is the file handle, not a blocking IO op against a slow disk
                            async for chunk in response.aiter_bytes(chunk_size=8192):
                                total += len(chunk)
                                if total > self._max_file_size:
                                    raise DownloadError(
                                        f"Download exceeded max size: {self._max_file_size} bytes"
                                    )
                                f.write(chunk)
                        break
                else:
                    raise DownloadError(
                        f"Too many redirects (> {_MAX_REDIRECTS}) downloading "
                        f"{redact_url_credentials(url)}"
                    )

        except httpx.HTTPStatusError as exc:
            dest.unlink(missing_ok=True)
            raise DownloadError(
                redact_text(f"HTTP {exc.response.status_code} downloading {url}")
            ) from exc
        except httpx.HTTPError as exc:
            dest.unlink(missing_ok=True)
            raise DownloadError(redact_text(f"Failed to download {url}: {exc}")) from exc
        except DownloadError, SSRFError:
            dest.unlink(missing_ok=True)
            raise

        logger.info("Downloaded %d bytes to %s", dest.stat().st_size, dest)
        return dest

    def _validate_magic_bytes(self, path: Path) -> None:
        """Validate that the file starts with PK magic bytes (ZIP/APK/IPA)."""
        try:
            with path.open("rb") as f:
                header = f.read(2)
        except OSError as exc:
            raise ValidationError(f"Cannot read file {path}: {exc}") from exc

        if header != _PK_MAGIC:
            path.unlink(missing_ok=True)
            raise ValidationError(
                f"Invalid file: expected PK (ZIP) header, got {header!r}. "
                "The file may be corrupted or not a valid APK/IPA."
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _detect_platform_from_ext(ext: str) -> str:
    """Detect platform from file extension."""
    if ext in {".ipa", ".app"}:
        return "ios"
    return "android"
