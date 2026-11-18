"""Unit tests for RedisClient and module-level helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.cache.redis_client import RedisClient

# ============================================================================
# Serialization / Deserialization
# ============================================================================


class TestSerialization:
    def test_serialize_dict(self):
        client = RedisClient.__new__(RedisClient)
        result = client._serialize({"key": "value"})
        assert json.loads(result) == {"key": "value"}

    def test_serialize_pydantic_model(self):
        model = MagicMock()
        model.model_dump.return_value = {"field": 42}
        client = RedisClient.__new__(RedisClient)
        result = client._serialize(model)
        assert json.loads(result) == {"field": 42}

    def test_deserialize_valid_json(self):
        client = RedisClient.__new__(RedisClient)
        result = client._deserialize('{"key": "value"}')
        assert result == {"key": "value"}

    def test_deserialize_none(self):
        client = RedisClient.__new__(RedisClient)
        assert client._deserialize(None) is None

    def test_deserialize_invalid_json(self):
        client = RedisClient.__new__(RedisClient)
        assert client._deserialize("not json {{{") is None


# ============================================================================
# Connection management
# ============================================================================


class TestEnsureConnected:
    def test_raises_when_not_connected(self):
        client = RedisClient.__new__(RedisClient)
        client._client = None
        with pytest.raises(RuntimeError, match="not connected"):
            client._ensure_connected()

    def test_returns_client_when_connected(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = MagicMock()
        client._client = mock_redis
        assert client._ensure_connected() is mock_redis


class TestConnect:
    @patch("w2v_common.cache.redis_client.ConnectionPool")
    @patch("w2v_common.cache.redis_client.redis.Redis")
    async def test_connect_creates_pool_and_client(self, mock_redis_cls, mock_pool_cls):
        mock_pool = MagicMock()
        mock_pool_cls.from_url.return_value = mock_pool

        mock_client = MagicMock()
        mock_client.ping = AsyncMock(return_value=True)
        mock_redis_cls.return_value = mock_client

        client = RedisClient(redis_url="redis://localhost:6379/0")
        await client.connect()

        assert client._pool is mock_pool
        assert client._client is mock_client
        mock_pool_cls.from_url.assert_called_once()

    @patch("w2v_common.cache.redis_client.ConnectionPool")
    @patch("w2v_common.cache.redis_client.redis.Redis")
    async def test_connect_skips_if_already_connected(self, mock_redis_cls, mock_pool_cls):
        client = RedisClient(redis_url="redis://localhost:6379/0")
        client._pool = MagicMock()
        client._client = MagicMock()

        await client.connect()
        mock_pool_cls.from_url.assert_not_called()


class TestDisconnect:
    async def test_disconnect_closes_client_and_pool(self):
        client = RedisClient(redis_url="redis://localhost:6379/0")
        mock_client = AsyncMock()
        mock_pool = AsyncMock()
        client._client = mock_client
        client._pool = mock_pool

        await client.disconnect()

        mock_client.aclose.assert_awaited_once()
        mock_pool.aclose.assert_awaited_once()
        assert client._client is None
        assert client._pool is None

    async def test_disconnect_noop_when_not_connected(self):
        client = RedisClient(redis_url="redis://localhost:6379/0")
        client._client = None
        client._pool = None
        await client.disconnect()  # should not raise


# ============================================================================
# Async context manager
# ============================================================================


class TestAsyncContextManager:
    async def test_aenter_aexit(self):
        client = RedisClient(redis_url="redis://localhost:6379/0")

        with (
            patch.object(client, "connect", new_callable=AsyncMock) as mock_connect,
            patch.object(client, "disconnect", new_callable=AsyncMock) as mock_disconnect,
        ):
            async with client as c:
                assert c is client
                mock_connect.assert_awaited_once()

            mock_disconnect.assert_awaited_once()


# ============================================================================
# CRUD operations
# ============================================================================


class TestGet:
    async def test_get_returns_deserialized(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value='{"status":"ok"}')
        client._client = mock_redis

        result = await client.get("key1")
        assert result == {"status": "ok"}

    async def test_get_returns_none_for_missing(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        client._client = mock_redis

        result = await client.get("missing")
        assert result is None

    async def test_get_returns_none_on_redis_error(self):
        import redis.exceptions

        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=redis.exceptions.ConnectionError("connection lost"))
        client._client = mock_redis

        result = await client.get("key1")
        assert result is None

    async def test_get_propagates_non_redis_error(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=TypeError("unexpected type"))
        client._client = mock_redis

        with pytest.raises(TypeError, match="unexpected type"):
            await client.get("key1")


class TestSet:
    async def test_set_without_ttl(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        client._client = mock_redis

        result = await client.set("key1", {"data": 1})
        assert result is True
        mock_redis.set.assert_awaited_once()

    async def test_set_with_ttl(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(return_value=True)
        client._client = mock_redis

        result = await client.set("key1", {"data": 1}, ttl=60)
        assert result is True
        mock_redis.setex.assert_awaited_once_with("key1", 60, '{"data": 1}')

    async def test_set_returns_false_on_redis_error(self):
        import redis.exceptions

        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=redis.exceptions.ConnectionError("broken"))
        client._client = mock_redis

        result = await client.set("key1", "val")
        assert result is False

    async def test_set_propagates_non_redis_error(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=TypeError("bad type"))
        client._client = mock_redis

        with pytest.raises(TypeError):
            await client.set("key1", "val")


class TestDelete:
    async def test_delete_success(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(return_value=1)
        client._client = mock_redis

        assert await client.delete("key1") is True

    async def test_delete_returns_false_on_redis_error(self):
        import redis.exceptions

        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(side_effect=redis.exceptions.ConnectionError("fail"))
        client._client = mock_redis

        assert await client.delete("key1") is False


class TestExists:
    async def test_exists_true(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=1)
        client._client = mock_redis

        assert await client.exists("key1") is True

    async def test_exists_false(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=0)
        client._client = mock_redis

        assert await client.exists("key1") is False

    async def test_exists_error_returns_false(self):
        import redis.exceptions

        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(side_effect=redis.exceptions.ConnectionError("fail"))
        client._client = mock_redis

        assert await client.exists("key1") is False


class TestPing:
    async def test_ping_success(self):
        client = RedisClient.__new__(RedisClient)
        mock_redis = MagicMock()
        ping_coro = AsyncMock(return_value=True)
        mock_redis.ping = ping_coro
        client._client = mock_redis

        result = await client.ping()
        assert result is True

    async def test_ping_error_returns_false(self):
        import redis.exceptions

        client = RedisClient.__new__(RedisClient)
        mock_redis = MagicMock()
        mock_redis.ping = MagicMock(side_effect=redis.exceptions.ConnectionError("down"))
        client._client = mock_redis

        result = await client.ping()
        assert result is False


# ============================================================================
# Module-level helpers
# ============================================================================


class TestModuleLevelHelpers:
    @patch("w2v_common.cache.redis_client._redis_client", None)
    @patch("w2v_common.cache.redis_client.get_redis_client")
    async def test_init_redis(self, mock_get):
        from src.cache.redis_client import init_redis

        mock_client = AsyncMock()
        mock_get.return_value = mock_client

        result = await init_redis()
        mock_client.connect.assert_awaited_once()
        assert result is mock_client

    @patch("w2v_common.cache.redis_client._redis_client")
    async def test_close_redis_when_connected(self, mock_client):
        import w2v_common.cache.redis_client as mod

        client = AsyncMock()
        mod._redis_client = client

        await mod.close_redis()
        client.disconnect.assert_awaited_once()
        assert mod._redis_client is None

    async def test_close_redis_when_not_connected(self):
        import w2v_common.cache.redis_client as mod

        mod._redis_client = None
        await mod.close_redis()  # should not raise
