"""File storage service for app inspection artifacts.

Thin domain wrapper over the fleet-shared
:class:`w2v_common.storage.StorageService` facade. The byte-level I/O
primitives (``store_bytes`` / ``retrieve`` / ``presign``) and base
initialisation delegate to the shared facade; this wrapper keeps the
app-inspector key conventions (per-job directories, screenshot sub-dirs), the
startup/health **write-probe** for the local scratch volume, the retrieve
**path-confinement** guard, and the **streaming** recording upload/download that
the generic facade's in-memory ``put_bytes`` / ``get_bytes`` do not provide.

Multi-replica safety: with ``STORAGE_BACKEND=minio`` (the prod default, see
gitops ``app-inspector-service-config``) inspection artifacts are written to the
``w2v-app-inspector`` MinIO bucket instead of a pod-local ``emptyDir``, so a
screenshot written by one replica and a recording download served by another
replica both resolve. The ``local`` backend is retained for dev/test.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from w2v_common.storage import StorageService as _SharedStorageService

from src.config import get_settings

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

# Stream video downloads in 64 KiB chunks — bounded memory on long recordings
# without paying syscall overhead per packet (matches the orchestrator's
# artifact-proxy chunk size).
_STREAM_CHUNK_BYTES = 64 * 1024

# MinIO response headers we forward back to the client so ``<video>`` Range
# seeks and conditional fetches work. Whitelist only.
_FORWARDED_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
        "cache-control",
    }
)


class RecordingStream:
    """A streaming handle over a MinIO ``get_object`` response.

    Wraps the ``miniopy_async`` (aiohttp) response so a route can build a
    ``StreamingResponse`` without importing MinIO internals. ``body()`` yields
    the object in chunks and releases the upstream connection when the client
    disconnects (or the stream is exhausted).
    """

    def __init__(self, upstream: Any) -> None:
        self._upstream = upstream
        self.status: int = getattr(upstream, "status", 200)
        raw_headers = dict(getattr(upstream, "headers", {}) or {})
        self.headers: dict[str, str] = {
            k.lower(): v for k, v in raw_headers.items() if k.lower() in _FORWARDED_RESPONSE_HEADERS
        }

    async def body(self) -> AsyncIterator[bytes]:
        """Yield the recording in bounded chunks, then release the connection."""
        try:
            async for chunk in self._upstream.content.iter_chunked(_STREAM_CHUNK_BYTES):
                yield chunk
        finally:
            self._upstream.close()
            with contextlib.suppress(Exception):
                await self._upstream.release()


class StorageService:
    """Manages storage for inspection screenshots and recordings."""

    def __init__(self) -> None:
        settings = get_settings()
        self._local_path = Path(settings.storage.local_path)
        # Honour the configured backend (``local`` for dev/test, ``minio`` in
        # prod). The facade builds the MinIO client from this service's own
        # StorageSettings (per-service bucket + creds isolation).
        self._facade = _SharedStorageService(settings.storage)

    @property
    def local_path(self) -> Path:
        """Expose the configured local scratch root (read-only)."""
        return self._local_path

    @property
    def backend(self) -> str:
        """Active storage backend (``"local"`` or ``"minio"``)."""
        return self._facade.backend

    @property
    def bucket(self) -> str:
        """Configured MinIO bucket name."""
        return self._facade.bucket

    @property
    def _minio_client(self) -> Any:
        """The facade's lazily-initialised MinIO client (``None`` for local).

        Reaching the facade's client mirrors the fleet artifact-proxy pattern
        (``getattr(storage, "_minio_client", None)``) so the streaming
        recording paths reuse the single client the facade already owns rather
        than constructing a second one.
        """
        return getattr(self._facade, "_minio_client", None)

    async def initialize(self) -> None:
        """Initialise the backend, plus a write-probe of the local scratch dir.

        The facade ensures the bucket (minio) or the local dir. The local
        scratch dir is used regardless of backend (recordings are captured to
        disk before being streamed to MinIO), so we always write-probe it: a
        misconfigured k8s volume mount — directory present but not writable —
        must surface as a loud startup error instead of a silent success.
        """
        await self._facade.initialize()
        self._local_path.mkdir(parents=True, exist_ok=True)
        probe = self._local_path / f".startup_probe_{uuid.uuid7().hex}"
        try:
            probe.write_text("ok")
        finally:
            probe.unlink(missing_ok=True)
        logger.info("Storage initialized (backend=%s, scratch=%s)", self.backend, self._local_path)

    async def health_check(self) -> bool:
        """Return True if storage is usable right now.

        MinIO: the bucket is reachable (delegated to the facade). Local: the
        scratch dir is writable *right now* via a write-probe (write + unlink of
        a uniquely-named file) rather than ``os.access(..., W_OK)``, which lies
        on volumes mounted with extended ACLs.
        """
        if self.backend == "minio":
            return await self._facade.check_health()
        try:
            self._local_path.mkdir(parents=True, exist_ok=True)
            probe = self._local_path / f".health_check_{uuid.uuid7().hex}"
            probe.write_text("ok")
            probe.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def get_job_dir(self, job_id: str) -> Path:
        """Return the local scratch directory for a job, creating it if needed.

        This is a working directory on the pod: automation clients (Appium)
        write screenshots + the raw recording here during a run, after which
        those files are streamed/uploaded to shared storage.
        """
        job_dir = self._local_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir

    def get_screenshots_dir(self, job_id: str) -> Path:
        """Return the local scratch screenshots sub-directory for a job."""
        ss_dir = self.get_job_dir(job_id) / "screenshots"
        ss_dir.mkdir(parents=True, exist_ok=True)
        return ss_dir

    async def store_bytes(self, job_id: str, data: bytes, filename: str) -> str:
        """Store raw bytes under the job's key space. Returns the storage ref.

        Suitable for small objects (screenshots). For potentially-large
        recordings use :meth:`store_recording`, which streams from disk.

        Returns:
            The reference :meth:`retrieve` reads back — a MinIO object key
            (minio backend) or an absolute path (local backend).
        """
        ref = await self._facade.put_bytes(f"{job_id}/{filename}", data)
        logger.debug("Stored %d bytes to %s", len(data), ref)
        return ref

    async def store_recording(self, job_id: str, source_path: Path) -> str:
        """Persist a recording captured to ``source_path`` into shared storage.

        MinIO: ``fput_object`` streams the file from disk to the bucket in
        chunks (constant memory — never buffers the whole video through an
        in-memory put), then the pod-local scratch copy is removed. Local: the
        file already lives on the mounted scratch volume, so its path is
        returned as-is.

        Returns:
            The reference stored on ``InspectionJob.video_path`` — a MinIO
            object key (minio) or an absolute path (local).
        """
        if self.backend == "minio":
            key = f"{job_id}/recording.mp4"
            await self._minio_client.fput_object(
                self.bucket,
                key,
                str(source_path),
                content_type="video/mp4",
            )
            logger.info("Streamed recording to MinIO: %s/%s", self.bucket, key)
            with contextlib.suppress(OSError):
                source_path.unlink()
            return key
        logger.info("Recording retained on local scratch: %s", source_path)
        return str(source_path)

    def _safe_local_path(self, raw: str) -> Path | None:
        """Resolve ``raw`` and reject anything outside ``self._local_path``.

        Defense in depth for the local backend: a rejection means a stale row
        from a different configuration or a tampered DB entry — return ``None``
        (treated as "not found") rather than honouring the traversal.
        """
        root = self._local_path.resolve()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._local_path / raw
        try:
            resolved = candidate.resolve()
        except OSError, RuntimeError:
            logger.warning("Could not resolve storage path: %s", raw)
            return None
        if not resolved.is_relative_to(root):
            logger.warning("Rejected path outside storage root: %s", raw)
            return None
        return resolved

    async def retrieve(self, ref: str) -> bytes | None:
        """Read an object's bytes by the reference a ``store_*`` returned.

        MinIO: read the object key via the facade. Local: path-confined read.
        Returns ``None`` if absent.
        """
        if self.backend == "minio":
            return await self._facade.get_bytes(ref)
        file_path = self._safe_local_path(ref)
        if file_path is None:
            return None
        return await self._facade.get_bytes(str(file_path))

    async def presign(self, ref: str, *, expires: int | None = None) -> str | None:
        """Presigned GET URL for a stored object (MinIO only); ``None`` otherwise."""
        return await self._facade.presign_get(ref, expires=expires)

    async def open_recording_stream(
        self, ref: str, *, request_headers: dict[str, str] | None = None
    ) -> RecordingStream | None:
        """Open a streaming MinIO ``get_object`` for a recording.

        Returns a :class:`RecordingStream` (constant-memory chunked body), or
        ``None`` when the object is absent / the backend is not MinIO.
        ``request_headers`` (e.g. ``Range``) are forwarded so ``<video>`` seeks
        work. Unexpected transport errors propagate to the caller.
        """
        client = self._minio_client
        if client is None:
            return None
        try:
            from miniopy_async.error import S3Error
        except ImportError:  # pragma: no cover -- a minio-backend consumer always has it
            return None
        try:
            upstream = await client.get_object(
                self.bucket, ref, request_headers=request_headers or None
            )
        except S3Error as exc:
            status = getattr(exc, "status_code", 500) or 500
            if status in (403, 404):
                logger.info("Recording object absent in MinIO: %s (status=%s)", ref, status)
                return None
            raise
        return RecordingStream(upstream)

    async def list_screenshots(self, job_id: str) -> list[str]:
        """List stored screenshot references for a job, sorted.

        MinIO: object keys under ``<job_id>/screenshots/``. Local: absolute
        PNG paths in the scratch screenshots sub-directory.
        """
        if self.backend == "minio":
            keys = await self._facade.list_prefix(f"{job_id}/screenshots/")
            return sorted(k for k in keys if k.endswith(".png"))
        ss_dir = self.get_screenshots_dir(job_id)
        return sorted(str(f) for f in ss_dir.glob("*.png"))


def get_storage_service(request: Request) -> StorageService | None:
    """Return the app-state ``StorageService`` (``None`` if not initialised)."""
    return getattr(request.app.state, "storage_service", None)
