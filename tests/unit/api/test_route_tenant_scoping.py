"""Every by-id inspection-job lookup must be scoped to the caller's tenant.

Each route is exercised three ways against a real postgres:

1. ``job_a`` as ``tenant-a`` → 200 (proves the fixture is real, so a blanket
   404 cannot fake a pass),
2. ``job_b`` (owned by ``tenant-b``) as ``tenant-a`` → 404 whose body is
   byte-identical to a genuine not-found (no 403 — that is an enumeration
   oracle over uuid7 ids),
3. a never-seeded id → 404, the same body.

The status route reads a tenant-keyed Redis cache *before* the DB, so the
cache can never serve a foreign tenant's entry — the leak is purely the DB
fallback. The redis fake is therefore flushed between legs (a stale cached
200 must not be able to fake either result) and one case spies on
``JobManager.get_job`` to prove the DB path really was taken.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware
from w2v_common.asgi import register_error_envelope_handlers
from w2v_common.cache.redis_client import RedisClient
from w2v_common.testing import inject_fakeredis

from src.services.job_manager import JobManager
from tests.conftest import TENANT_APP, TENANT_OTHER

# w2v-common 0.67.0: tenant identity is a uuid; the routes 401 a slug, so the
# fixture tenants must be real uuids or every case below would 401 instead of
# exercising the 200/404 scoping split this file exists to prove.
TENANT_A = TENANT_APP
TENANT_B = TENANT_OTHER
FIXED_REQUEST_ID = "req-tenant-scope-fixed"
FROZEN_TS = "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def frozen_error_envelope(monkeypatch):
    """Freeze the error-envelope timestamp so 404 bodies compare byte-for-byte.

    ``request_id`` is pinned per-request via the ``X-Request-Id`` header; the
    timestamp is the only other non-deterministic field in the envelope.
    """
    import w2v_common.asgi.error_envelope as envelope

    monkeypatch.setattr(envelope, "_utc_timestamp", lambda: FROZEN_TS)


@pytest.fixture
def fake_redis():
    """RedisClient backed by fakeredis; the raw fake is returned for flushing."""
    client = RedisClient(redis_url="redis://localhost:6379/0")
    fake = inject_fakeredis(client)
    return client, fake


class _TenantMiddleware(BaseHTTPMiddleware):
    """Stand in for AuthMiddleware: bind ``request.state.tenant_id`` per request."""

    async def dispatch(self, request: Request, call_next):
        # No header = no identity at all. Deliberately leaves state UNSET
        # rather than substituting a sentinel, which is what the route now
        # turns into a 401.
        header = request.headers.get("x-test-tenant")
        if header is not None:
            request.state.tenant_id = header
        return await call_next(request)


@pytest.fixture
async def client(db_session, fake_redis, monkeypatch):
    """ASGI client over the real router, a real DB session, and fakeredis."""
    redis_client, _fake = fake_redis
    monkeypatch.setattr("src.api.routes.jobs.get_redis_client", lambda: redis_client)

    from src.api.routes import router as api_router
    from src.database.session import get_db

    app = FastAPI(redirect_slashes=False)
    app.add_middleware(_TenantMiddleware)
    app.include_router(api_router)
    register_error_envelope_handlers(app, debug=False)

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture
async def seeded_jobs(db_session, sample_screens_data, sample_nav_graph, tmp_path):
    """Two completed jobs: ``job_a`` owned by tenant-a, ``job_b`` by tenant-b."""
    ids = {}
    for key, tenant in (("a", TENANT_A), ("b", TENANT_B)):
        job = await JobManager.create_job(
            db_session,
            tenant_id=tenant,
            app_path=f"/tmp/{key}.apk",
            platform="android",
        )
        video = tmp_path / f"{key}.mp4"
        video.write_bytes(f"video-{key}".encode())
        job.status = "completed"
        job.screens_data = sample_screens_data
        job.navigation_graph = sample_nav_graph
        job.screens_discovered = len(sample_screens_data)
        job.video_path = str(video)
        ids[key] = job.job_id
    await db_session.commit()
    return ids


@pytest.fixture
def missing_id():
    """An id that was never seeded (uuid7 — the real id shape)."""
    return str(uuid.uuid7())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(tenant: str) -> dict[str, str]:
    return {"x-test-tenant": tenant, "x-request-id": FIXED_REQUEST_ID}


def _expected_404(genuine_body: bytes, genuine_id: str, actual_id: str) -> bytes:
    """The genuine not-found body rewritten for a different echoed job id.

    The 404 message echoes the requested id, so "byte-identical" means: same
    bytes once the echoed id is the one that was actually requested. Every
    other field (code, status, request_id, timestamp) must match exactly.
    """
    return genuine_body.replace(genuine_id.encode(), actual_id.encode())


async def _assert_scoped_get(client, path_for, seeded_jobs, missing_id, *, assert_ok):
    """Run the 200 / foreign-404 / missing-404 matrix for a GET route."""
    ok = await client.get(path_for(seeded_jobs["a"]), headers=_headers(TENANT_A))
    assert ok.status_code == 200, ok.text
    assert_ok(ok)

    missing = await client.get(path_for(missing_id), headers=_headers(TENANT_A))
    assert missing.status_code == 404, missing.text

    foreign = await client.get(path_for(seeded_jobs["b"]), headers=_headers(TENANT_A))
    assert foreign.status_code == 404, foreign.text
    assert foreign.content == _expected_404(missing.content, missing_id, seeded_jobs["b"])


# ---------------------------------------------------------------------------
# jobs.py — status / result / screens / video
# ---------------------------------------------------------------------------


async def test_status_is_tenant_scoped_and_hits_db(
    client, seeded_jobs, missing_id, fake_redis, monkeypatch
):
    """Status must scope by tenant, and prove the DB fallback was the gate.

    The status cache key already carries the tenant, so a foreign caller
    always misses it — flush between legs anyway so no leg can be served by a
    cached response, and spy on ``JobManager.get_job`` so the 404 is proven to
    come from a scoped DB read, not merely from an absent cache key.
    """
    _client, fake = fake_redis
    calls: list[tuple[str, str | None]] = []
    original = JobManager.get_job

    async def _spy(db, job_id, **kwargs):
        calls.append((job_id, kwargs.get("tenant_id")))
        return await original(db, job_id, **kwargs)

    monkeypatch.setattr(JobManager, "get_job", staticmethod(_spy))

    await fake.flushall()
    ok = await client.get(f"/api/inspect/{seeded_jobs['a']}", headers=_headers(TENANT_A))
    assert ok.status_code == 200, ok.text
    assert ok.json()["job_id"] == seeded_jobs["a"]

    await fake.flushall()
    missing = await client.get(f"/api/inspect/{missing_id}", headers=_headers(TENANT_A))
    assert missing.status_code == 404, missing.text

    await fake.flushall()
    calls.clear()
    foreign = await client.get(f"/api/inspect/{seeded_jobs['b']}", headers=_headers(TENANT_A))
    assert foreign.status_code == 404, foreign.text
    assert foreign.content == _expected_404(missing.content, missing_id, seeded_jobs["b"])
    # The DB path was taken (not a bare cache miss) and it was tenant-scoped.
    assert calls == [(seeded_jobs["b"], TENANT_A)]


async def test_status_cache_cannot_serve_a_foreign_tenant(client, seeded_jobs, fake_redis):
    """Tenant-b warming its own cache entry must not make it visible to tenant-a."""
    _client, fake = fake_redis
    await fake.flushall()
    warm = await client.get(f"/api/inspect/{seeded_jobs['b']}", headers=_headers(TENANT_B))
    assert warm.status_code == 200, warm.text

    foreign = await client.get(f"/api/inspect/{seeded_jobs['b']}", headers=_headers(TENANT_A))
    assert foreign.status_code == 404, foreign.text


async def test_result_is_tenant_scoped(client, seeded_jobs, missing_id, fake_redis):
    _client, fake = fake_redis
    await fake.flushall()
    await _assert_scoped_get(
        client,
        lambda jid: f"/api/inspect/{jid}/result",
        seeded_jobs,
        missing_id,
        assert_ok=lambda r: r.json()["job_id"] == seeded_jobs["a"],
    )


async def test_screens_is_tenant_scoped(client, seeded_jobs, missing_id, fake_redis):
    _client, fake = fake_redis
    await fake.flushall()
    await _assert_scoped_get(
        client,
        lambda jid: f"/api/inspect/{jid}/screens",
        seeded_jobs,
        missing_id,
        assert_ok=lambda r: r.json()[0]["screen_id"] == "screen_0",
    )


async def test_video_is_tenant_scoped(client, seeded_jobs, missing_id, fake_redis):
    """A foreign tenant must not stream another tenant's session recording."""
    _client, fake = fake_redis
    await fake.flushall()
    await _assert_scoped_get(
        client,
        lambda jid: f"/api/inspect/{jid}/video",
        seeded_jobs,
        missing_id,
        assert_ok=lambda r: r.content == b"video-a",
    )


# ---------------------------------------------------------------------------
# export.py
# ---------------------------------------------------------------------------


async def test_export_is_tenant_scoped(client, seeded_jobs, missing_id, fake_redis):
    _client, fake = fake_redis
    await fake.flushall()
    await _assert_scoped_get(
        client,
        lambda jid: f"/api/inspect/{jid}/export/html",
        seeded_jobs,
        missing_id,
        assert_ok=lambda r: seeded_jobs["a"] in r.text,
    )


# ---------------------------------------------------------------------------
# selection.py — GET preview + POST select (a cross-tenant WRITE)
# ---------------------------------------------------------------------------


async def test_screens_preview_is_tenant_scoped(client, seeded_jobs, missing_id, fake_redis):
    _client, fake = fake_redis
    await fake.flushall()
    await _assert_scoped_get(
        client,
        lambda jid: f"/api/inspect/{jid}/screens/preview",
        seeded_jobs,
        missing_id,
        assert_ok=lambda r: r.json()[0]["screen_id"] == "screen_0",
    )


async def test_select_screens_is_tenant_scoped_and_refuses_the_write(
    client, db_session, seeded_jobs, missing_id, fake_redis
):
    """The foreign POST must 404 *and* leave the victim's row untouched.

    A 404/409 status alone does not prove the write did not land — re-read the
    row and assert both ``selected_screen_ids`` and ``status`` are unchanged.
    """
    from sqlalchemy import select

    from src.database.models import InspectionJob

    _client, fake = fake_redis
    await fake.flushall()

    # Both jobs must be selectable for the write path to be reachable.
    for job_id in seeded_jobs.values():
        job = await JobManager.get_job(db_session, job_id)
        assert job is not None
        job.status = "awaiting_selection"
    await db_session.commit()

    body = {"selected_screen_ids": ["screen_0"]}

    ok = await client.post(
        f"/api/inspect/{seeded_jobs['a']}/screens/select",
        json=body,
        headers=_headers(TENANT_A),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["selected_count"] == 1

    missing = await client.post(
        f"/api/inspect/{missing_id}/screens/select",
        json=body,
        headers=_headers(TENANT_A),
    )
    assert missing.status_code == 404, missing.text

    foreign = await client.post(
        f"/api/inspect/{seeded_jobs['b']}/screens/select",
        json=body,
        headers=_headers(TENANT_A),
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.content == _expected_404(missing.content, missing_id, seeded_jobs["b"])

    # Prove the write was refused, not merely reported as refused.
    db_session.expire_all()
    victim = (
        await db_session.execute(
            select(InspectionJob).where(InspectionJob.job_id == seeded_jobs["b"])
        )
    ).scalar_one()
    assert victim.selected_screen_ids is None
    assert victim.status == "awaiting_selection"


async def test_update_screen_selection_write_predicate_is_load_bearing(db_session, seeded_jobs):
    """The UPDATE itself must be tenant-keyed — proven WITHOUT the route.

    The route's scoped read already 404s a foreign job_id, so driving this
    through HTTP would only re-prove the read guard. Call the manager directly
    with tenant-a's id against tenant-b's row: the write must match zero rows
    (``JobStateConflictError``) and leave the victim untouched.
    """
    from sqlalchemy import select

    from src.database.models import InspectionJob
    from src.services.job_manager import JobStateConflictError

    victim_id = seeded_jobs["b"]
    job = await JobManager.get_job(db_session, victim_id)
    assert job is not None
    job.status = "awaiting_selection"
    await db_session.commit()

    with pytest.raises(JobStateConflictError):
        await JobManager.update_screen_selection(
            db_session, victim_id, ["screen_0"], tenant_id=TENANT_A
        )

    db_session.expire_all()
    victim = (
        await db_session.execute(select(InspectionJob).where(InspectionJob.job_id == victim_id))
    ).scalar_one()
    assert victim.selected_screen_ids is None
    assert victim.status == "awaiting_selection"

    # ...and the owning tenant's write still lands (the predicate scopes, not blocks).
    await JobManager.update_screen_selection(
        db_session, victim_id, ["screen_0"], tenant_id=TENANT_B
    )
    db_session.expire_all()
    victim = (
        await db_session.execute(select(InspectionJob).where(InspectionJob.job_id == victim_id))
    ).scalar_one()
    assert victim.selected_screen_ids == ["screen_0"]
    assert victim.status == "completed"


async def test_select_screens_call_site_forwards_caller_tenant(
    client, db_session, seeded_jobs, fake_redis, monkeypatch
):
    """The route must actually pass ``tenant_id`` through to the manager write.

    The scoped read (``get_job``) already 404s a foreign tenant, so it alone
    cannot prove the write call itself is tenant-scoped — deleting the
    ``tenant_id=tenant_id`` kwarg at the call site would still leave every
    other test in this file green. Spy on
    ``JobManager.update_screen_selection`` with a recording wrapper and
    assert it was invoked with the caller's tenant.
    """
    _client, fake = fake_redis
    await fake.flushall()

    job = await JobManager.get_job(db_session, seeded_jobs["a"])
    assert job is not None
    job.status = "awaiting_selection"
    await db_session.commit()

    calls: list[dict] = []
    original = JobManager.update_screen_selection

    async def _spy(db, job_id, selected_screen_ids, **kwargs):
        calls.append({"job_id": job_id, "tenant_id": kwargs.get("tenant_id")})
        return await original(db, job_id, selected_screen_ids, **kwargs)

    monkeypatch.setattr(JobManager, "update_screen_selection", staticmethod(_spy))

    ok = await client.post(
        f"/api/inspect/{seeded_jobs['a']}/screens/select",
        json={"selected_screen_ids": ["screen_0"]},
        headers=_headers(TENANT_A),
    )
    assert ok.status_code == 200, ok.text
    assert calls == [{"job_id": seeded_jobs["a"], "tenant_id": TENANT_A}]


async def test_select_screens_state_conflict_returns_409_not_500(
    client, db_session, seeded_jobs, fake_redis
):
    """A genuine state-race at the manager layer must surface as 409, not 500.

    The route's precheck allows both ``completed`` and ``awaiting_selection``
    through, but the manager's atomic UPDATE only ever matches
    ``awaiting_selection`` rows. Re-submitting a selection on an
    already-``completed`` job passes the precheck and then loses the race at
    the manager, raising ``JobStateConflictError`` — which selection.py must
    catch and turn into the fleet's 409 error envelope.
    """
    _client, fake = fake_redis
    await fake.flushall()

    job = await JobManager.get_job(db_session, seeded_jobs["a"])
    assert job is not None
    job.status = "completed"
    job.selected_screen_ids = ["screen_0"]
    await db_session.commit()

    resp = await client.post(
        f"/api/inspect/{seeded_jobs['a']}/screens/select",
        json={"selected_screen_ids": ["screen_0"]},
        headers=_headers(TENANT_A),
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "HTTP_409"
    assert body["error"]["status"] == 409
    assert "request_id" in body["error"]
    assert "timestamp" in body["error"]


async def test_select_screens_cross_tenant_race_returns_409_not_500(
    client, db_session, seeded_jobs, fake_redis, monkeypatch
):
    """A tenant-mismatch race at the write layer must also surface as 409.

    This models the window the tenant predicate on ``update_screen_selection``
    exists for: the scoped read succeeds, but by write-time the row no longer
    matches the caller's tenant (e.g. a concurrent reassignment), so the
    UPDATE matches zero rows and raises ``JobStateConflictError`` exactly as
    the state-conflict case does. The route must not let this leak as a 500
    either.
    """
    _client, fake = fake_redis
    await fake.flushall()

    job = await JobManager.get_job(db_session, seeded_jobs["a"])
    assert job is not None
    job.status = "awaiting_selection"
    await db_session.commit()

    from src.services.job_manager import JobStateConflictError

    async def _raise_conflict(db, job_id, selected_screen_ids, **kwargs):
        raise JobStateConflictError(
            f"Inspection job {job_id!r} is not awaiting_selection — cannot apply screen selection"
        )

    monkeypatch.setattr(JobManager, "update_screen_selection", staticmethod(_raise_conflict))

    resp = await client.post(
        f"/api/inspect/{seeded_jobs['a']}/screens/select",
        json={"selected_screen_ids": ["screen_0"]},
        headers=_headers(TENANT_A),
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "HTTP_409"
    assert body["error"]["status"] == 409


# ---------------------------------------------------------------------------
# compare.py — BOTH job-id slots must be scoped, one at a time
# ---------------------------------------------------------------------------


async def _compare(client, tenant, baseline, comparison):
    return await client.post(
        "/api/inspect/compare",
        json={"baseline_job_id": baseline, "comparison_job_id": comparison},
        headers=_headers(tenant),
    )


async def test_compare_scopes_the_baseline_slot(client, seeded_jobs, missing_id, fake_redis):
    _client, fake = fake_redis
    await fake.flushall()

    ok = await _compare(client, TENANT_A, seeded_jobs["a"], seeded_jobs["a"])
    assert ok.status_code == 200, ok.text
    assert "summary" in ok.json()

    missing = await _compare(client, TENANT_A, missing_id, seeded_jobs["a"])
    assert missing.status_code == 404, missing.text

    foreign = await _compare(client, TENANT_A, seeded_jobs["b"], seeded_jobs["a"])
    assert foreign.status_code == 404, foreign.text
    assert foreign.content == _expected_404(missing.content, missing_id, seeded_jobs["b"])


async def test_compare_scopes_the_comparison_slot(client, seeded_jobs, missing_id, fake_redis):
    _client, fake = fake_redis
    await fake.flushall()

    ok = await _compare(client, TENANT_A, seeded_jobs["a"], seeded_jobs["a"])
    assert ok.status_code == 200, ok.text

    missing = await _compare(client, TENANT_A, seeded_jobs["a"], missing_id)
    assert missing.status_code == 404, missing.text

    foreign = await _compare(client, TENANT_A, seeded_jobs["a"], seeded_jobs["b"])
    assert foreign.status_code == 404, foreign.text
    assert foreign.content == _expected_404(missing.content, missing_id, seeded_jobs["b"])
