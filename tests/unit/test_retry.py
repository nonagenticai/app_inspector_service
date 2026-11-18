"""Unit tests for async retry with backoff."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.helpers.retry import async_retry_with_backoff


class TestAsyncRetryWithBackoff:
    async def test_succeeds_first_try(self):
        func = AsyncMock(return_value="ok")
        result = await async_retry_with_backoff(func, max_retries=3)
        assert result == "ok"
        func.assert_awaited_once()

    @patch("src.helpers.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_failure(self, mock_sleep):
        func = AsyncMock(side_effect=[ValueError("fail"), "ok"])
        result = await async_retry_with_backoff(func, max_retries=2, base_delay=0.01)
        assert result == "ok"
        assert func.await_count == 2
        mock_sleep.assert_awaited_once()

    @patch("src.helpers.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_exhausts_retries(self, mock_sleep):
        func = AsyncMock(side_effect=ValueError("always fails"))
        with pytest.raises(ValueError, match="always fails"):
            await async_retry_with_backoff(func, max_retries=2, base_delay=0.01)
        assert func.await_count == 3  # initial + 2 retries

    @patch("src.helpers.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_only_retries_specified_exceptions(self, mock_sleep):
        func = AsyncMock(side_effect=TypeError("wrong type"))
        with pytest.raises(TypeError, match="wrong type"):
            await async_retry_with_backoff(
                func,
                max_retries=3,
                retryable_exceptions=(ValueError,),
                base_delay=0.01,
            )
        func.assert_awaited_once()  # Should not retry TypeError

    @patch("src.helpers.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_passes_args_and_kwargs(self, mock_sleep):
        func = AsyncMock(return_value="result")
        result = await async_retry_with_backoff(func, "arg1", "arg2", max_retries=1, key="value")
        assert result == "result"
        func.assert_awaited_once_with("arg1", "arg2", key="value")
