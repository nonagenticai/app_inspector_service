"""Redis-based sliding window rate limiter."""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException, Request
from prometheus_client import Counter
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from src.cache.redis_client import RedisClient, get_redis_client
from src.config import get_settings

logger = logging.getLogger(__name__)

# A dropped/stale Redis socket must degrade this GUARD, not the request it
# guards (see issue #93). Tracked separately from the generic
# "redis unavailable" warning log so an operator can see the fail-open rate
# on /metrics without grepping logs.
rate_limiter_failopen_total = Counter(
    "w2v_app_inspector_rate_limiter_failopen_total",
    "Requests let through unthrottled because Redis was unreachable, by reason.",
    ["reason"],
)

# Transient connection faults: the socket is dead/timed-out, not a real Redis
# error. Retried once against a fresh connection before failing open.
_TRANSIENT_REDIS_ERRORS = (RedisConnectionError, RedisTimeoutError)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-tenant sliding window rate limiter using Redis."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Enforce per-tenant rate limits on inspection creation requests."""
        settings = get_settings()

        # Only rate-limit inspection creation
        if request.method != "POST" or not request.url.path.rstrip("/").endswith("/api/inspect"):
            return await call_next(request)

        tenant_id = getattr(request.state, "tenant_id", "anonymous")
        requests_per_minute = settings.rate_limit.requests_per_minute
        concurrent_limit = settings.rate_limit.concurrent_jobs_per_tenant

        redis_client: RedisClient | None = None
        try:
            redis_client = get_redis_client()
            await self._enforce_limits(
                redis_client, tenant_id, requests_per_minute, concurrent_limit
            )
        except HTTPException:
            raise
        except _TRANSIENT_REDIS_ERRORS as e:
            # The pool's socket may have gone stale while idle (e.g. Redis
            # restarted underneath a long-lived connection). Reconnect once
            # against a fresh socket and retry the whole check before giving
            # up and failing open — a single reboot/failover should not cost
            # every in-flight pod an unthrottled window until its NEXT
            # request happens to reconnect. ``redis_client`` is always bound
            # here: only ``_enforce_limits`` (called after assignment) raises
            # this exception type, never ``get_redis_client()`` itself — the
            # `if` is defensive, not an expected branch.
            logger.warning(
                "Rate limiter: Redis connection error (%s), reconnecting and retrying once",
                e,
            )
            try:
                if redis_client is None:
                    raise e
                await self._retry_after_reconnect(
                    redis_client, tenant_id, requests_per_minute, concurrent_limit
                )
            except HTTPException:
                raise
            except _TRANSIENT_REDIS_ERRORS as retry_exc:
                logger.warning(
                    "Rate limiter failed (Redis unavailable after retry), "
                    "allowing request unthrottled: %s",
                    retry_exc,
                )
                rate_limiter_failopen_total.labels(reason="connection_error").inc()
        except (OSError, RuntimeError, RedisError) as e:
            # RuntimeError: client was never connected (_ensure_connected).
            # OSError: lower-level socket/DNS failures redis-py doesn't wrap.
            # RedisError: ⚠️ the base of redis-py's OWN hierarchy, and it derives
            #   from Exception — NOT from OSError. Without it, every non-transient
            #   server-side reply escaped this middleware and 500'd the request it
            #   was supposed to be guarding, which is the exact inverse of the
            #   contract stated at the top of this module. The reachable cases are
            #   not exotic: a Redis at ``maxmemory`` answers the ``zadd`` below
            #   with ``ResponseError: OOM command not allowed when used memory >
            #   'maxmemory'``, and a replica promoted read-only answers
            #   ``ResponseError: READONLY You can't write against a read only
            #   replica``. Both took POST /api/inspect from "unthrottled" to
            #   "totally unavailable", and because the counter below never
            #   incremented, /metrics showed nothing at all.
            logger.warning("Rate limiter failed (Redis unavailable), allowing request: %s", e)
            rate_limiter_failopen_total.labels(reason="unavailable").inc()

        return await call_next(request)

    @staticmethod
    async def _retry_after_reconnect(
        redis_client: RedisClient,
        tenant_id: str,
        requests_per_minute: int,
        concurrent_limit: int,
    ) -> None:
        """Force a fresh connection pool, then retry the check exactly once.

        ``RedisClient.connect()`` is a no-op once ``_pool`` is set, so a bare
        retry against the SAME client would hand back the identical stale
        socket. ``disconnect()`` first is what actually forces new sockets.
        """
        await redis_client.disconnect()
        await redis_client.connect()
        await RateLimitMiddleware._enforce_limits(
            redis_client, tenant_id, requests_per_minute, concurrent_limit
        )

    @staticmethod
    async def _enforce_limits(
        redis_client: RedisClient,
        tenant_id: str,
        requests_per_minute: int,
        concurrent_limit: int,
    ) -> None:
        """Run the sliding-window + concurrent-job checks, or raise HTTPException(429)."""
        client = redis_client._ensure_connected()

        now = time.time()
        window_key = f"app_inspector:ratelimit:{tenant_id}:window"

        # Remove expired entries
        await client.zremrangebyscore(window_key, 0, now - 60)

        # Count current window
        current_count = await client.zcard(window_key)

        if current_count >= requests_per_minute:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {requests_per_minute} requests per minute",
            )

        # Add current request
        await client.zadd(window_key, {str(now): now})
        await client.expire(window_key, 120)

        # Check concurrent jobs
        concurrent_key = f"app_inspector:concurrent:{tenant_id}"
        concurrent_count = await client.get(concurrent_key)
        if concurrent_count and int(concurrent_count) >= concurrent_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Concurrent job limit exceeded: {concurrent_limit} jobs",
            )
