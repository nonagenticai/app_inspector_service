"""Tests for ``StorageService`` — real-filesystem write probe.

These tests run against ``tmp_path`` so the write-probe pattern used by
``initialize`` / ``health_check`` is exercised end-to-end. Mocking those
paths would hide the exact class of bug the probe exists to catch.
"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.services.storage_service import StorageService


def _probe(svc: StorageService) -> bool:
    """Run one ``health_check`` to completion on THIS thread.

    ``health_check``'s local branch never awaits, so it cannot overlap with
    itself on a single event loop; running each probe on its own thread is what
    makes the concurrency in ``test_concurrent_probes_do_not_clash`` real.
    """
    return asyncio.run(svc.health_check())


def _make_storage(local_path: Path) -> StorageService:
    """Build a StorageService whose root is a throwaway tmp_path directory."""
    fake_settings = MagicMock()
    fake_settings.storage.backend = "local"
    fake_settings.storage.local_path = str(local_path)
    with patch("src.services.storage_service.get_settings", return_value=fake_settings):
        return StorageService()


class TestInitialize:
    async def test_creates_missing_root_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "app_inspector_root"
        assert not root.exists()

        svc = _make_storage(root)
        await svc.initialize()

        assert root.exists()
        # Write-probe cleans up after itself.
        assert list(root.iterdir()) == []

    async def test_is_idempotent(self, tmp_path: Path) -> None:
        svc = _make_storage(tmp_path)
        await svc.initialize()
        await svc.initialize()
        assert list(tmp_path.iterdir()) == []

    async def test_raises_on_unwritable_directory(self, tmp_path: Path) -> None:
        """A mounted-but-read-only directory must surface at startup, not at
        first write. The write-probe is what makes the k8s misconfiguration
        path visible — without it, ``initialize()`` silently succeeds.
        """
        if os.geteuid() == 0:  # root bypasses file permissions
            pytest.skip("Test requires a non-root process")

        read_only = tmp_path / "ro"
        read_only.mkdir()
        read_only.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x------
        try:
            svc = _make_storage(read_only)
            with pytest.raises(OSError):
                await svc.initialize()
        finally:
            # Restore writability so pytest can clean up the tmp_path tree.
            read_only.chmod(stat.S_IRWXU)


class TestHealthCheck:
    async def test_reports_healthy_for_writable_directory(self, tmp_path: Path) -> None:
        svc = _make_storage(tmp_path)
        assert await svc.health_check() is True
        # Probe file must not linger.
        remaining = [p.name for p in tmp_path.iterdir()]
        assert remaining == []

    async def test_reports_unhealthy_for_read_only_directory(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("Test requires a non-root process")

        read_only = tmp_path / "ro"
        read_only.mkdir()
        read_only.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            svc = _make_storage(read_only)
            assert await svc.health_check() is False
        finally:
            read_only.chmod(stat.S_IRWXU)

    async def test_creates_missing_directory_during_probe(self, tmp_path: Path) -> None:
        """``health_check`` is self-healing: a missing root is recreated."""
        missing = tmp_path / "never_existed"
        svc = _make_storage(missing)
        assert await svc.health_check() is True
        assert missing.exists()
        assert list(missing.iterdir()) == []

    async def test_concurrent_probes_do_not_clash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrent ``health_check`` calls must not race on probe filenames.

        ⚠️ How this test is built is load-bearing. ``health_check``'s local
        branch is entirely SYNCHRONOUS file IO inside an ``async def``, so a
        plain ``asyncio.gather`` over 20 calls never suspends between the write
        and the unlink and runs them strictly one-at-a-time — nothing can
        clash. Worse, the outcome the old version asserted (``all(results)``
        plus an empty directory) is reached even with a HARD-CODED probe name,
        because ``unlink(missing_ok=True)`` swallows the loser's error. Proven
        by mutation on 2026-09-04: replacing the uuid7 suffix with a fixed
        ``.health_check_FIXED`` left this test GREEN.

        So this asserts the property directly instead: every concurrent probe
        must claim its OWN path. The probes run in real threads
        (``asyncio.to_thread``) so the IO genuinely overlaps, and a spy on
        ``Path.write_text`` records which path each one claimed.
        """
        claimed: list[str] = []
        claimed_lock = threading.Lock()
        real_write_text = Path.write_text

        def _spy(
            self: Path,
            data: str,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> int:
            if self.name.startswith(".health_check"):
                with claimed_lock:
                    claimed.append(self.name)
            return real_write_text(self, data, encoding, errors, newline)

        monkeypatch.setattr(Path, "write_text", _spy)

        svc = _make_storage(tmp_path)
        results = await asyncio.gather(*[asyncio.to_thread(_probe, svc) for _ in range(20)])
        assert all(results)
        # THE property: 20 probes, 20 distinct filenames. A shared name here is
        # one probe unlinking another's live file.
        assert len(claimed) == 20
        assert len(set(claimed)) == 20, f"probe filenames collided: {sorted(claimed)}"
        # All probes should be gone.
        assert list(tmp_path.iterdir()) == []


class TestRetrievePathConfinement:
    """Defense-in-depth: ``retrieve`` must refuse paths outside storage root."""

    async def test_legitimate_roundtrip_works(self, tmp_path: Path) -> None:
        svc = _make_storage(tmp_path)
        await svc.initialize()
        stored_path = await svc.store_bytes("job-1", b"payload", "data.bin")
        data = await svc.retrieve(stored_path)
        assert data == b"payload"

    async def test_rejects_absolute_outside_root(self, tmp_path: Path) -> None:
        svc = _make_storage(tmp_path / "root")
        await svc.initialize()
        # Plant a target file outside the storage root.
        outside = tmp_path / "secret.txt"
        outside.write_bytes(b"sensitive")
        assert await svc.retrieve(str(outside)) is None
        # Target file untouched.
        assert outside.read_bytes() == b"sensitive"

    async def test_rejects_traversal(self, tmp_path: Path) -> None:
        svc = _make_storage(tmp_path / "root")
        await svc.initialize()
        outside = tmp_path / "secret.txt"
        outside.write_bytes(b"sensitive")
        assert await svc.retrieve("../secret.txt") is None
        assert outside.read_bytes() == b"sensitive"


def _make_minio_storage(tmp_path: Path, *, bucket: str = "w2v-app-inspector") -> StorageService:
    """Build a MinIO-backed StorageService WITHOUT initialising a real client.

    The wrapper reaches the facade's ``_minio_client`` for streaming ops; tests
    inject a fake client onto that attribute rather than touching a live MinIO.
    """
    from src.config import StorageSettings

    minio_settings = StorageSettings(
        backend="minio",
        minio_endpoint="minio.test:8389",
        minio_bucket=bucket,
        local_path=str(tmp_path),
    )
    fake_settings = MagicMock()
    fake_settings.storage = minio_settings
    with patch("src.services.storage_service.get_settings", return_value=fake_settings):
        return StorageService()


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeMinioResponse:
    def __init__(self, chunks: list[bytes], headers: dict[str, str], status: int = 200) -> None:
        self.status = status
        self.headers = headers
        self.content = _FakeContent(chunks)
        self.closed = False
        self.released = False

    def close(self) -> None:
        self.closed = True

    async def release(self) -> None:
        self.released = True


class _FakeMinioClient:
    def __init__(self) -> None:
        self.fput_calls: list[tuple] = []
        self._get_response: _FakeMinioResponse | None = None

    async def fput_object(self, bucket: str, key: str, path: str, content_type: str = "") -> None:
        self.fput_calls.append((bucket, key, path, content_type))

    async def get_object(self, bucket: str, key: str, request_headers=None):
        return self._get_response


def test_minio_backend_is_honoured(tmp_path: Path) -> None:
    """STORAGE_BACKEND=minio must be honoured so inspection artifacts land in the
    shared bucket (multi-replica safety) — the wrapper no longer forces local."""
    svc = _make_minio_storage(tmp_path, bucket="w2v-app-inspector")
    assert svc.backend == "minio"
    assert svc.bucket == "w2v-app-inspector"


class TestMinioRecording:
    async def test_store_recording_streams_from_disk(self, tmp_path: Path) -> None:
        svc = _make_minio_storage(tmp_path)
        fake = _FakeMinioClient()
        svc._facade._minio_client = fake  # inject the streaming client

        source = tmp_path / "recording.mp4"
        source.write_bytes(b"MP4DATA")

        ref = await svc.store_recording("job-9", source)

        # Uploaded via a chunked fput (streamed from disk), NOT an in-memory put.
        assert ref == "job-9/recording.mp4"
        assert fake.fput_calls == [
            ("w2v-app-inspector", "job-9/recording.mp4", str(source), "video/mp4")
        ]
        # Pod-local scratch copy is removed after the stream upload.
        assert not source.exists()

    async def test_open_recording_stream_yields_chunks_and_releases(self, tmp_path: Path) -> None:
        svc = _make_minio_storage(tmp_path)
        fake = _FakeMinioClient()
        response = _FakeMinioResponse(
            chunks=[b"aaa", b"bbb"],
            headers={"Content-Type": "video/mp4", "Content-Length": "6", "x-amz-junk": "drop"},
        )
        fake._get_response = response
        svc._facade._minio_client = fake

        stream = await svc.open_recording_stream("job-9/recording.mp4")
        assert stream is not None
        # Only whitelisted headers survive.
        assert stream.headers.get("content-type") == "video/mp4"
        assert "x-amz-junk" not in stream.headers

        body = b"".join([chunk async for chunk in stream.body()])
        assert body == b"aaabbb"
        # Upstream connection released after the body is exhausted.
        assert response.closed is True
        assert response.released is True

    async def test_open_recording_stream_none_when_client_absent(self, tmp_path: Path) -> None:
        svc = _make_minio_storage(tmp_path)
        # No client injected (facade never initialised) → absent, not a crash.
        assert svc._facade._minio_client is None
        assert await svc.open_recording_stream("job-9/recording.mp4") is None
