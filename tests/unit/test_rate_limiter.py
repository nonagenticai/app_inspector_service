"""Unit tests for RateLimitMiddleware."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError

from src.middleware.rate_limiter import RateLimitMiddleware


def _make_request(method: str = "POST", path: str = "/api/inspect") -> MagicMock:
    request = MagicMock()
    request.method = method
    request.url.path = path
    request.state = MagicMock()
    request.state.tenant_id = "01950000-0000-7000-8000-00000000ffff"
    return request


class TestRateLimitMiddleware:
    @patch("src.middleware.rate_limiter.get_settings")
    async def test_non_post_requests_passthrough(self, mock_settings):
        mw = RateLimitMiddleware(app=MagicMock())
        request = _make_request(method="GET")
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        response = await mw.dispatch(request, call_next)
        assert response is expected

    @patch("src.middleware.rate_limiter.get_settings")
    async def test_non_inspect_path_passthrough(self, mock_settings):
        mw = RateLimitMiddleware(app=MagicMock())
        request = _make_request(method="POST", path="/api/other")
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        response = await mw.dispatch(request, call_next)
        assert response is expected

    @patch("src.middleware.rate_limiter.get_redis_client")
    @patch("src.middleware.rate_limiter.get_settings")
    async def test_under_limit_allows_request(self, mock_settings, mock_get_redis):
        mock_settings.return_value = MagicMock(
            rate_limit=MagicMock(requests_per_minute=30, concurrent_jobs_per_tenant=5)
        )

        mock_redis_raw = AsyncMock()
        mock_redis_raw.zremrangebyscore = AsyncMock()
        mock_redis_raw.zcard = AsyncMock(return_value=5)
        mock_redis_raw.zadd = AsyncMock()
        mock_redis_raw.expire = AsyncMock()
        mock_redis_raw.get = AsyncMock(return_value=None)

        mock_redis_client = MagicMock()
        mock_redis_client._ensure_connected.return_value = mock_redis_raw
        mock_get_redis.return_value = mock_redis_client

        mw = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        response = await mw.dispatch(request, call_next)
        assert response is expected

    @patch("src.middleware.rate_limiter.get_redis_client")
    @patch("src.middleware.rate_limiter.get_settings")
    async def test_rate_limit_exceeded_raises_429(self, mock_settings, mock_get_redis):
        mock_settings.return_value = MagicMock(
            rate_limit=MagicMock(requests_per_minute=5, concurrent_jobs_per_tenant=10)
        )

        mock_redis_raw = AsyncMock()
        mock_redis_raw.zremrangebyscore = AsyncMock()
        mock_redis_raw.zcard = AsyncMock(return_value=5)  # At limit

        mock_redis_client = MagicMock()
        mock_redis_client._ensure_connected.return_value = mock_redis_raw
        mock_get_redis.return_value = mock_redis_client

        mw = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        call_next = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await mw.dispatch(request, call_next)
        assert exc_info.value.status_code == 429
        assert "Rate limit" in exc_info.value.detail

    @patch("src.middleware.rate_limiter.get_redis_client")
    @patch("src.middleware.rate_limiter.get_settings")
    async def test_concurrent_limit_exceeded_raises_429(self, mock_settings, mock_get_redis):
        mock_settings.return_value = MagicMock(
            rate_limit=MagicMock(requests_per_minute=100, concurrent_jobs_per_tenant=2)
        )

        mock_redis_raw = AsyncMock()
        mock_redis_raw.zremrangebyscore = AsyncMock()
        mock_redis_raw.zcard = AsyncMock(return_value=1)  # Under rate limit
        mock_redis_raw.zadd = AsyncMock()
        mock_redis_raw.expire = AsyncMock()
        mock_redis_raw.get = AsyncMock(return_value="2")  # At concurrent limit

        mock_redis_client = MagicMock()
        mock_redis_client._ensure_connected.return_value = mock_redis_raw
        mock_get_redis.return_value = mock_redis_client

        mw = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        call_next = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await mw.dispatch(request, call_next)
        assert exc_info.value.status_code == 429
        assert "Concurrent" in exc_info.value.detail

    @patch("src.middleware.rate_limiter.get_redis_client")
    @patch("src.middleware.rate_limiter.get_settings")
    async def test_redis_failure_allows_request(self, mock_settings, mock_get_redis):
        """When Redis is down, requests should still go through."""
        mock_settings.return_value = MagicMock(
            rate_limit=MagicMock(requests_per_minute=30, concurrent_jobs_per_tenant=5)
        )
        mock_get_redis.side_effect = OSError("Redis down")

        mw = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        response = await mw.dispatch(request, call_next)
        assert response is expected

    @patch("src.middleware.rate_limiter.get_redis_client")
    @patch("src.middleware.rate_limiter.get_settings")
    async def test_stale_socket_connection_error_fails_open(self, mock_settings, mock_get_redis):
        """Regression test for #93: a dropped/stale Redis socket raising
        redis.exceptions.ConnectionError mid-check (not just at client
        acquisition) must fail the guard OPEN, not 500 the request. The
        middleware retries once against a fresh connection; this test keeps
        the retry failing too, so it must reach the fail-open path.
        """
        mock_settings.return_value = MagicMock(
            rate_limit=MagicMock(requests_per_minute=30, concurrent_jobs_per_tenant=5)
        )

        mock_redis_raw = AsyncMock()
        mock_redis_raw.zremrangebyscore = AsyncMock(
            side_effect=RedisConnectionError("Connection closed by server.")
        )

        mock_redis_client = MagicMock()
        mock_redis_client._ensure_connected.return_value = mock_redis_raw
        mock_redis_client.disconnect = AsyncMock()
        mock_redis_client.connect = AsyncMock()
        mock_get_redis.return_value = mock_redis_client

        mw = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        response = await mw.dispatch(request, call_next)

        assert response is expected
        call_next.assert_awaited_once()
        # Reconnect was attempted before failing open.
        mock_redis_client.disconnect.assert_awaited_once()
        mock_redis_client.connect.assert_awaited_once()
        assert mock_redis_raw.zremrangebyscore.await_count == 2  # initial + retry

    @pytest.mark.parametrize(
        ("message", "case"),
        [
            ("OOM command not allowed when used memory > 'maxmemory'.", "maxmemory"),
            ("READONLY You can't write against a read only replica.", "readonly_replica"),
        ],
    )
    @patch("src.middleware.rate_limiter.get_redis_client")
    @patch("src.middleware.rate_limiter.get_settings")
    async def test_server_side_redis_error_fails_open(
        self, mock_settings, mock_get_redis, message: str, case: str
    ):
        """A server-side Redis reply must degrade the GUARD, not 500 the request.

        ``redis.exceptions.ResponseError`` derives from ``RedisError`` ->
        ``Exception`` — NOT from ``OSError``, and it is not one of the two
        transient types. With the handler's tuple limited to
        ``(OSError, RuntimeError)`` this escaped ``dispatch`` entirely and every
        POST /api/inspect returned 500 — the exact inverse of this module's
        stated contract — while ``rate_limiter_failopen_total`` stayed at zero,
        so /metrics showed nothing wrong. Both messages below are verbatim
        real-server replies: a Redis at ``maxmemory``, and a replica promoted
        read-only by a failover.
        """
        from redis.exceptions import ResponseError

        mock_settings.return_value = MagicMock(
            rate_limit=MagicMock(requests_per_minute=30, concurrent_jobs_per_tenant=5)
        )

        mock_redis_raw = AsyncMock()
        mock_redis_raw.zremrangebyscore = AsyncMock(return_value=0)
        mock_redis_raw.zcard = AsyncMock(return_value=0)
        mock_redis_raw.zadd = AsyncMock(side_effect=ResponseError(message))

        mock_redis_client = MagicMock()
        mock_redis_client._ensure_connected.return_value = mock_redis_raw
        mock_get_redis.return_value = mock_redis_client

        mw = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        response = await mw.dispatch(request, call_next)

        assert response is expected, f"{case}: request was not allowed through"
        call_next.assert_awaited_once()
