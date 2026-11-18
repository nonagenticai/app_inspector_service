"""Shared test fixtures for app_inspector_service."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from w2v_common.cache.redis_client import RedisClient
from w2v_common.testing import inject_fakeredis

# Capture the test DB URL BEFORE the autouse mock_env_vars fixture
# monkeypatches DATABASE_URL to its in-test default. CI sets DATABASE_URL
# to its app_inspector_service_ci postgres; locally falls back to the default
# below (developer must run a local postgres to execute db_engine tests).
_TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://test:test@localhost:5432/test",  # pragma: allowlist secret
)

# w2v_common >= 0.15.0 dropped the localhost defaults on DatabaseSettings.url,
# RedisSettings.url, and StorageSettings.minio_endpoint. Settings() is
# instantiated at module import time during pytest collection, BEFORE the
# autouse mock_env_vars fixture runs; seed module-level so collection
# succeeds. The autouse fixture re-applies the same placeholders after its
# delenv sweep.
os.environ.setdefault("DATABASE_URL", _TEST_DATABASE_URL)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("STORAGE_MINIO_ENDPOINT", "localhost:9000")
# AUTH_ENABLED must be seeded at module load (before any test imports src.asgi)
# because src.asgi runs ``register_middleware(app, AuthMiddleware)`` at import
# time when ``settings.auth.enabled`` is true. Tests that import the app
# eagerly (test_openapi.py) would otherwise lock in AuthMiddleware before the
# autouse mock_env_vars fixture can clear it.
os.environ.setdefault("AUTH_ENABLED", "false")


# ---------------------------------------------------------------------------
# Tenant identity (w2v-common 0.67.0)
# ---------------------------------------------------------------------------
# 0.67.0 made tenant identity a real ``uuid.UUID`` end to end and deleted the
# nil-UUID substitution, so a slug like "test-tenant" is no longer a tenant
# anywhere: pydantic refuses it on ``User.tenant_id``, the AuthMiddleware 401s
# an introspection payload carrying one, and ``set_identity_context`` raises.
# Fixtures use these real uuids instead. They are distinct so a cross-tenant
# assertion still proves scoping rather than accidentally comparing a tenant to
# itself.
TENANT_APP = "11111111-1111-4111-8111-111111111111"
TENANT_OTHER = "22222222-2222-4222-8222-222222222222"

# Every spelling ``uuid.UUID`` accepts for the all-zeros value. The retired
# fail-safe wrote the dashed form, so a string compare against that one literal
# would let the other three straight through.
NIL_TENANT_SPELLINGS = (
    "00000000-0000-0000-0000-000000000000",
    "00000000000000000000000000000000",
    "{00000000-0000-0000-0000-000000000000}",
    "urn:uuid:00000000-0000-0000-0000-000000000000",
)


def attach_test_tenant(app: Any, tenant_id: str = TENANT_APP) -> Any:
    """Stand in for ``AuthMiddleware`` on a bare test app.

    Routes read ``request.state.tenant_id`` and 401 when it is absent (there is
    no fallback tenant any more), so a test app that mounts a router WITHOUT
    ``w2v_common``'s AuthMiddleware has to bind identity itself.
    """

    @app.middleware("http")
    async def _set_tenant(request: Any, call_next: Any) -> Any:
        request.state.tenant_id = tenant_id
        return await call_next(request)

    return app


def asgi_tenant_wrapper(app: Any, tenant_id: str = TENANT_APP) -> Any:
    """Same stand-in as :func:`attach_test_tenant`, but as an ASGI wrapper.

    ``src.asgi.app`` is a module-level singleton shared by every integration
    test, and Starlette refuses ``add_middleware`` once an app has served a
    request — so the decorator form above cannot be used on it. Wrapping
    instead mutates nothing: ``Request.state`` reads ``scope["state"]``, so
    seeding that key is exactly what AuthMiddleware would have done.
    """

    class _TenantScopedApp:
        # Attribute access delegates to the real app so callers can still reach
        # ``client.app.state`` — the wrapper must be invisible except for the
        # one scope key it seeds.
        def __getattr__(self, name: str) -> Any:
            return getattr(app, name)

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope.get("type") == "http":
                scope.setdefault("state", {})["tenant_id"] = tenant_id
            await app(scope, receive, send)

    return _TenantScopedApp()


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """Remove CI env vars that interfere with config-default tests, then set test defaults."""
    for key in list(os.environ):
        if any(
            key.startswith(p)
            for p in (
                "DATABASE_",
                "AUTH_",
                "REDIS_",
                "API_",
                "STORAGE_",
                "LOG_",
                "CACHE_",
                "TRACING_",
                "LLM_",
                "FFMPEG_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://test:test@localhost:5432/test",  # pragma: allowlist secret
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", "/tmp/test_app_inspector")
    monkeypatch.setenv("STORAGE_MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("ENVIRONMENT", "testing")


@pytest.fixture
def mock_db_session():
    """Mock async database session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def mock_redis_client():
    """Mock Redis client."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock(return_value=True)
    client.delete = AsyncMock(return_value=True)
    client.exists = AsyncMock(return_value=False)
    client.ping = AsyncMock(return_value=True)
    return client


@pytest.fixture
def sample_screens_data():
    """Sample screens data as stored in JSONB."""
    return [
        {
            "screen_id": "screen_0",
            "screen_type": "login",
            "activity_name": "com.example.LoginActivity",
            "text_content": "Welcome Sign In Username Password",
            "interactive_elements": 3,
            "accessibility_ids": ["username_field", "password_field", "login_button"],
            "screenshot_path": "/tmp/test/screenshots/screen_0.png",
            "headings": ["Welcome"],
            "has_forms": True,
            "has_media": False,
            "content_sections": [],
            "accessibility_issues": [
                {
                    "rule": "missing_content_description",
                    "severity": "warning",
                    "element_tag": "android.widget.ImageView",
                    "element_text": "",
                    "message": "ImageView missing content description",
                }
            ],
            "accessibility_score": 85,
            "interactive_elements_detail": [
                {
                    "tag": "android.widget.EditText",
                    "text": "Username",
                    "content_desc": "",
                    "resource_id": "username_field",
                    "clickable": False,
                }
            ],
            "ui_components": [
                {
                    "component_type": "text_input",
                    "tag": "android.widget.EditText",
                    "confidence": 1.0,
                    "resource_id": "username_field",
                }
            ],
        },
        {
            "screen_id": "screen_1",
            "screen_type": "content",
            "activity_name": "com.example.MainActivity",
            "text_content": "Home Dashboard Recent Items",
            "interactive_elements": 5,
            "accessibility_ids": [],
            "screenshot_path": "/tmp/test/screenshots/screen_1.png",
            "headings": ["Home", "Dashboard"],
            "has_forms": False,
            "has_media": True,
            "content_sections": [
                {"heading": "Recent Items", "element_count": 3, "has_interactive": True}
            ],
            "accessibility_issues": [],
            "accessibility_score": 100,
            "interactive_elements_detail": [],
            "ui_components": [],
        },
    ]


@pytest.fixture
def sample_nav_graph():
    """Sample navigation graph as stored in JSONB."""
    return [
        {
            "from_screen": "screen_0",
            "to_screen": "screen_1",
            "action": "tap",
            "element_label": "login_button",
        }
    ]


# ---------------------------------------------------------------------------
# Real database fixtures (postgres+psycopg, TRUNCATE-based isolation)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine():
    """Per-test async postgres engine with TRUNCATE-based isolation.

    Connects to _TEST_DATABASE_URL captured at module load (CI postgres in
    CI, localhost fallback locally). Runs Base.metadata.create_all (idempotent)
    then TRUNCATEs every model table so each test starts from a clean slate.
    """
    from src.database.models import Base

    engine = create_async_engine(_TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if Base.metadata.sorted_tables:
            tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
            await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    """Real async_sessionmaker for app_inspector_service tests."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def db_session(db_session_factory):
    """Real AsyncSession for a single test."""
    async with db_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Real Redis fixtures (fakeredis)
# ---------------------------------------------------------------------------


@pytest.fixture
def real_redis_client():
    """RedisClient backed by fakeredis (real commands, in-memory)."""
    client = RedisClient(redis_url="redis://localhost:6379/0")
    inject_fakeredis(client)
    return client
