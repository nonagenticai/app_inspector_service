"""Unit tests for health check endpoints."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import redis.exceptions
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError, ProgrammingError

import src.api.health as health_mod
from src.api.health import health_check, health_live, health_ready, readyz


def _as_dict(value: dict[str, Any] | JSONResponse) -> dict[str, Any]:
    """Narrow a health check result to dict after isinstance verification."""
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _make_request(storage_service: Any = None) -> Request:
    """Build a minimal fake Request whose ``app.state.storage_service`` is set.

    The health endpoint only reads ``request.app.state.storage_service``, so
    a MagicMock spec'd to that attribute is sufficient.
    """
    request = MagicMock(spec=Request)
    app = MagicMock()
    state = MagicMock()
    if storage_service is None:
        storage_service = AsyncMock()
        storage_service.health_check = AsyncMock(return_value=True)
    state.storage_service = storage_service
    app.state = state
    request.app = app
    return cast(Request, request)


# ============================================================================
# /health/live
# ============================================================================


class TestHealthLive:
    async def test_live_returns_ok(self):
        result = await health_live()
        assert result == {"status": "ok"}


# ============================================================================
# /health (combined)
# ============================================================================


class TestHealthCheck:
    def setup_method(self):
        """Reset health cache before each test."""
        import src.api.health as health_mod

        health_mod._health_cache = {}
        health_mod._health_cache_ts = 0.0

    @patch("src.api.health.get_redis_client")
    @patch("src.api.health.get_engine")
    async def test_all_healthy(self, mock_engine, mock_redis):
        # DB healthy
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_engine_instance = AsyncMock()
        mock_engine_instance.connect = MagicMock(return_value=AsyncMock())
        mock_engine_instance.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine_instance.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.return_value = mock_engine_instance

        # Redis healthy
        mock_redis_client = AsyncMock()
        mock_redis_client.ping = AsyncMock(return_value=True)
        mock_redis.return_value = mock_redis_client

        result = _as_dict(await health_check(_make_request()))

        assert result["status"] == "healthy"
        components = result["components"]
        assert isinstance(components, dict)
        assert components["api"] == "healthy"
        assert components["storage"] == "healthy"

    @patch("src.api.health.get_redis_client")
    @patch("src.api.health.get_engine")
    async def test_db_unhealthy(self, mock_engine, mock_redis):
        mock_engine.side_effect = OperationalError("DB down", None, Exception("DB down"))

        mock_redis_client = AsyncMock()
        mock_redis_client.ping = AsyncMock(return_value=True)
        mock_redis.return_value = mock_redis_client

        result = _as_dict(await health_check(_make_request()))

        assert result["status"] == "degraded"
        components = result["components"]
        assert isinstance(components, dict)
        assert "unhealthy" in components["database"]

    @patch("src.api.health.get_redis_client")
    @patch("src.api.health.get_engine")
    async def test_redis_unhealthy(self, mock_engine, mock_redis):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_engine_instance = AsyncMock()
        mock_engine_instance.connect = MagicMock(return_value=AsyncMock())
        mock_engine_instance.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine_instance.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.return_value = mock_engine_instance

        mock_redis.side_effect = redis.exceptions.ConnectionError("Redis down")

        result = _as_dict(await health_check(_make_request()))

        assert result["status"] == "degraded"
        components = result["components"]
        assert isinstance(components, dict)
        assert "unhealthy" in components["redis"]

    @patch("src.api.health.get_redis_client")
    @patch("src.api.health.get_engine")
    async def test_storage_not_writable(self, mock_engine, mock_redis):
        """A storage service whose write-probe fails reports 'not writable'."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_engine_instance = AsyncMock()
        mock_engine_instance.connect = MagicMock(return_value=AsyncMock())
        mock_engine_instance.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine_instance.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.return_value = mock_engine_instance

        mock_redis_client = AsyncMock()
        mock_redis_client.ping = AsyncMock(return_value=True)
        mock_redis.return_value = mock_redis_client

        failing_storage = AsyncMock()
        failing_storage.health_check = AsyncMock(return_value=False)

        result = _as_dict(await health_check(_make_request(failing_storage)))

        assert result["status"] == "degraded"
        components = result["components"]
        assert isinstance(components, dict)
        assert components["storage"] == "not writable"

    @patch("src.api.health.get_redis_client")
    @patch("src.api.health.get_engine")
    async def test_storage_not_initialized(self, mock_engine, mock_redis):
        """If no storage service is attached to app.state, report 'not initialized'."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_engine_instance = AsyncMock()
        mock_engine_instance.connect = MagicMock(return_value=AsyncMock())
        mock_engine_instance.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine_instance.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.return_value = mock_engine_instance

        mock_redis_client = AsyncMock()
        mock_redis_client.ping = AsyncMock(return_value=True)
        mock_redis.return_value = mock_redis_client

        request = MagicMock(spec=Request)
        # app.state has no storage_service attribute at all
        request.app = MagicMock()
        request.app.state = MagicMock(spec=[])

        result = _as_dict(await health_check(cast(Request, request)))

        assert result["status"] == "degraded"
        components = result["components"]
        assert isinstance(components, dict)
        assert components["storage"] == "not initialized"

    @patch("src.api.health.get_redis_client")
    @patch("src.api.health.get_engine")
    async def test_cache_hit_skips_checks(self, mock_engine, mock_redis):
        """Second call within TTL returns cached result without DB call."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_engine_instance = AsyncMock()
        mock_engine_instance.connect = MagicMock(return_value=AsyncMock())
        mock_engine_instance.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine_instance.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.return_value = mock_engine_instance

        mock_redis_client = AsyncMock()
        mock_redis_client.ping = AsyncMock(return_value=True)
        mock_redis.return_value = mock_redis_client

        result1 = _as_dict(await health_check(_make_request()))

        assert result1["status"] == "healthy"
        assert mock_engine.call_count == 1

        # Second call -- should return cache, no additional engine call
        result2 = _as_dict(await health_check(_make_request()))
        assert result2["status"] == "healthy"
        assert mock_engine.call_count == 1  # Not called again

    @patch("src.api.health.get_redis_client")
    @patch("src.api.health.get_engine")
    async def test_cache_expires(self, mock_engine, mock_redis):
        """After TTL expires, checks are re-performed."""
        import src.api.health as health_mod

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_engine_instance = AsyncMock()
        mock_engine_instance.connect = MagicMock(return_value=AsyncMock())
        mock_engine_instance.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine_instance.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.return_value = mock_engine_instance

        mock_redis_client = AsyncMock()
        mock_redis_client.ping = AsyncMock(return_value=True)
        mock_redis.return_value = mock_redis_client

        await health_check(_make_request())
        assert mock_engine.call_count == 1

        # Simulate cache expiry by backdating the timestamp
        health_mod._health_cache_ts = time.monotonic() - 10.0

        await health_check(_make_request())
        assert mock_engine.call_count == 2


# ============================================================================
# /health/ready
# ============================================================================


def _engine_mock(*, connect_error: BaseException | None = None) -> AsyncMock:
    """Build an engine mock whose ``connect()`` is a real async context manager.

    Readiness must be decided by an actual round trip, so the mock has to model
    ``async with engine.connect() as conn: await conn.execute(...)``. When
    ``connect_error`` is given, entering that context raises it -- the mock
    stand-in for an unreachable/misconfigured database.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock()
    engine = AsyncMock()
    engine.pool = MagicMock()
    engine.pool.status.return_value = "Pool size: 10"
    engine.connect = MagicMock(return_value=AsyncMock())
    if connect_error is None:
        engine.connect.return_value.__aenter__ = AsyncMock(return_value=conn)
    else:
        engine.connect.return_value.__aenter__ = AsyncMock(side_effect=connect_error)
    engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
    return engine


class TestHealthReady:
    @patch("src.api.health.get_engine")
    async def test_ready_healthy(self, mock_engine):
        engine = _engine_mock()
        mock_engine.return_value = engine

        result = _as_dict(await health_ready())
        assert result["ready"] is True
        assert result["status"] == "healthy"
        checks = result["checks"]
        assert isinstance(checks, dict)
        assert "pool" in checks["database"]
        # The probe must be a real round trip, not an inference from the engine.
        engine.connect.assert_called_once()
        engine.connect.return_value.__aenter__.assert_awaited_once()

    @patch("src.api.health.get_engine")
    async def test_ready_db_down(self, mock_engine):
        mock_engine.side_effect = OperationalError(
            "connection refused", None, Exception("connection refused")
        )

        result = await health_ready()
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    @patch("src.api.health.get_engine")
    async def test_ready_connect_refused_is_not_ready(self, mock_engine):
        """A lazy engine that fails on first real connect must 503, not 200."""
        mock_engine.return_value = _engine_mock(
            connect_error=OperationalError(
                "connection refused", None, Exception("connection refused")
            )
        )

        result = await health_ready()
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503
        body = json.loads(bytes(result.body))
        assert body["ready"] is False
        assert body["checks"]["database"]["status"] == "down"

    @patch("src.api.health.get_engine")
    async def test_ready_programming_error_is_503_not_500(self, mock_engine):
        """Any SQLAlchemyError -- not just Operational/Interface -- must 503.

        ``ProgrammingError`` (a ``DBAPIError``/``SQLAlchemyError`` subclass, e.g.
        a missing relation or a revoked grant) is neither ``OperationalError``
        nor ``InterfaceError``. With a narrow catch tuple it escapes the handler
        and FastAPI turns it into a 500, which kubelet treats as a probe failure
        with an opaque body instead of a clean not-ready signal.
        """
        mock_engine.return_value = _engine_mock(
            connect_error=ProgrammingError(
                "SELECT 1", None, Exception('relation "x" does not exist')
            )
        )

        result = await health_ready()
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503
        body = json.loads(bytes(result.body))
        assert body["ready"] is False
        assert body["checks"]["database"]["status"] == "down"

    @patch("src.api.health.get_engine")
    async def test_ready_engine_creation_failure_is_not_ready(self, mock_engine):
        """``create_engine_with_retry`` raises ConnectionError once retries are spent."""
        mock_engine.side_effect = ConnectionError("Failed to connect to PostgreSQL")

        result = await health_ready()
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    @patch("src.api.health.get_engine")
    async def test_ready_hung_database_times_out(self, mock_engine, monkeypatch):
        """A DB that never answers fails readiness instead of hanging the probe."""
        monkeypatch.setattr(health_mod, "_READY_DB_TIMEOUT", 0.05)

        async def _never_answers() -> None:
            await asyncio.sleep(30)

        engine = _engine_mock()
        engine.connect.return_value.__aenter__ = AsyncMock(side_effect=_never_answers)
        mock_engine.return_value = engine

        result = await health_ready()
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    @patch("src.api.health.get_engine")
    async def test_readyz_alias_matches(self, mock_engine):
        mock_engine.side_effect = OperationalError(
            "connection refused", None, Exception("connection refused")
        )

        result = await readyz()
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503
