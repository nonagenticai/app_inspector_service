"""Recording-download route: multi-replica safety.

The recording is stored in the shared ``w2v-app-inspector`` MinIO bucket, so the
download endpoint must resolve it WITHOUT any pod-local file — the download can
land on a different replica than the one that captured the recording. These
tests drive ``get_job_video`` directly with a fake shared-storage backend and no
filesystem at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes import jobs as jobs_route
from tests.conftest import TENANT_APP


def _call(job_id: str, request: SimpleNamespace):
    """Invoke the route handler with the fake request/db cast to their types."""
    return jobs_route.get_job_video(
        job_id, cast(Request, request), db=cast(AsyncSession, AsyncMock())
    )


class _FakeStream:
    def __init__(self, chunks: list[bytes], headers: dict[str, str], status: int = 200) -> None:
        self.status = status
        self.headers = headers
        self._chunks = chunks

    async def body(self):
        for chunk in self._chunks:
            yield chunk


def _fake_request(storage: object) -> SimpleNamespace:
    return SimpleNamespace(
        headers={"range": "bytes=0-"},
        # The route scopes its job lookup to the caller's tenant, which it reads
        # off request.state (bound by AuthMiddleware in the real app).
        state=SimpleNamespace(tenant_id=TENANT_APP),
        app=SimpleNamespace(state=SimpleNamespace(storage_service=storage)),
    )


async def test_recording_streams_from_shared_store_without_local_disk(monkeypatch) -> None:
    job = SimpleNamespace(video_path="job-x/recording.mp4")
    monkeypatch.setattr(jobs_route.JobManager, "get_job", AsyncMock(return_value=job))

    stream = _FakeStream(
        chunks=[b"vid", b"eo"],
        headers={"content-type": "video/mp4", "content-length": "5"},
    )
    storage = SimpleNamespace(
        backend="minio",
        open_recording_stream=AsyncMock(return_value=stream),
    )
    request = _fake_request(storage)

    resp = await _call("job-x", request)

    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "video/mp4"
    assert resp.headers["content-disposition"] == 'attachment; filename="inspection_job-x.mp4"'
    # Range header forwarded to the shared store so <video> seeks work.
    storage.open_recording_stream.assert_awaited_once()
    fwd = storage.open_recording_stream.await_args.kwargs["request_headers"]
    assert fwd == {"range": "bytes=0-"}
    # Body streams straight from the shared store (no local file touched).
    collected: list[bytes] = []
    async for chunk in resp.body_iterator:
        collected.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    assert b"".join(collected) == b"video"


async def test_recording_404_when_object_absent_in_shared_store(monkeypatch) -> None:
    job = SimpleNamespace(video_path="job-x/recording.mp4")
    monkeypatch.setattr(jobs_route.JobManager, "get_job", AsyncMock(return_value=job))
    storage = SimpleNamespace(
        backend="minio",
        open_recording_stream=AsyncMock(return_value=None),  # absent
    )

    with pytest.raises(HTTPException) as exc:
        await _call("job-x", _fake_request(storage))
    assert exc.value.status_code == 404


async def test_recording_404_when_no_recording_path(monkeypatch) -> None:
    job = SimpleNamespace(video_path=None)
    monkeypatch.setattr(jobs_route.JobManager, "get_job", AsyncMock(return_value=job))
    storage = SimpleNamespace(backend="minio", open_recording_stream=AsyncMock())

    with pytest.raises(HTTPException) as exc:
        await _call("job-x", _fake_request(storage))
    assert exc.value.status_code == 404
