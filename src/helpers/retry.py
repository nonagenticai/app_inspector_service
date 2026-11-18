"""Async retry with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


async def async_retry_with_backoff[T](
    func: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple = (Exception,),
    **kwargs: Any,
) -> T:
    """Retry an async function with exponential backoff and jitter."""
    last_exception: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except retryable_exceptions as exc:
            last_exception = exc
            if attempt == max_retries:
                break
            delay = min(base_delay * (2**attempt) + random.uniform(0, 1), max_delay)  # noqa: S311 -- not used for security/crypto
            logger.warning(
                "Attempt %d/%d failed: %s. Retrying in %.1fs",
                attempt + 1,
                max_retries + 1,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    if last_exception is None:
        raise RuntimeError("max_retries must be >= 0")
    raise last_exception
