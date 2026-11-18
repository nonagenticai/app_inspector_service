"""Unit tests for database connection module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================================
# _ensure_database_exists
# ============================================================================


class TestEnsureDatabaseExists:
    @patch("w2v_common.database.connection.create_engine")
    async def test_database_already_exists(self, mock_create_engine):
        from src.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1  # DB exists
        mock_conn.execute.return_value = mock_result

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()
        mock_create_engine.return_value = mock_engine

        with patch("w2v_common.database.connection.DB_NAME", "test_db"):
            await _ensure_database_exists(max_retries=1)

        mock_conn.execute.assert_called_once()

    @patch("w2v_common.database.connection.create_engine")
    async def test_database_created(self, mock_create_engine):
        from src.database.connection import _ensure_database_exists

        mock_conn = MagicMock()

        check_result = MagicMock()
        check_result.scalar.return_value = None  # DB does not exist

        executed: list[str] = []

        def _execute(statement, *_args, **_kwargs):
            # A side_effect FUNCTION, not a fixed-length list: the ONLY thing that
            # matters is that call 1 is the existence probe and must return a result
            # whose .scalar() is None; every later call is provisioning DDL whose
            # return value is unused. A list pinned the exact statement COUNT, so a
            # legitimate extra statement in w2v_common (REVOKE CONNECT ... FROM
            # PUBLIC, the born-LOCKED policy) exhausted the iterator and the
            # StopIteration surfaced as "RuntimeError: coroutine raised StopIteration".
            executed.append(str(statement))
            return check_result if len(executed) == 1 else None

        mock_conn.execute = MagicMock(side_effect=_execute)

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()
        mock_create_engine.return_value = mock_engine

        with patch("w2v_common.database.connection.DB_NAME", "new_db"):
            await _ensure_database_exists(max_retries=1)

        # The point of this test: an absent database gets CREATED. Deliberately does
        # NOT assert the REVOKE text -- that would re-pin the exact statement list.
        assert executed, "no statement was executed at all"
        assert "pg_database" in executed[0], executed
        assert any("CREATE DATABASE new_db" in stmt for stmt in executed), executed

    async def test_invalid_db_name_raises(self):
        from src.database.connection import _ensure_database_exists

        with patch("w2v_common.database.connection.DB_NAME", "bad-name!"):
            with pytest.raises(ValueError, match="invalid characters"):
                await _ensure_database_exists()

    @patch("w2v_common.database.connection.create_engine")
    async def test_connection_retry_exhausted(self, mock_create_engine):
        from sqlalchemy.exc import OperationalError

        from src.database.connection import _ensure_database_exists

        mock_create_engine.side_effect = OperationalError("", (), Exception("refused"))

        with (
            patch("w2v_common.database.connection.DB_NAME", "test_db"),
            patch("w2v_common.database.connection.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await _ensure_database_exists(max_retries=1)


# ============================================================================
# get_engine
# ============================================================================


class TestGetEngine:
    @patch("w2v_common.database.connection.create_engine_with_retry")
    async def test_get_engine_creates_new(self, mock_create):
        import w2v_common.database.connection as mod

        mod._engine = None
        mock_engine = AsyncMock()
        mock_create.return_value = mock_engine

        result = await mod.get_engine()
        assert result is mock_engine
        mod._engine = None

    async def test_get_engine_returns_cached(self):
        import w2v_common.database.connection as mod

        mock_engine = MagicMock()
        mod._engine = mock_engine

        result = await mod.get_engine()
        assert result is mock_engine
        mod._engine = None


# ============================================================================
# health_check
# ============================================================================


class TestHealthCheck:
    @patch("w2v_common.database.connection.get_engine")
    async def test_health_check_healthy(self, mock_get_engine):
        from src.database.connection import health_check

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_engine = AsyncMock()
        mock_engine.begin = MagicMock(return_value=AsyncMock())
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_get_engine.return_value = mock_engine

        result = await health_check()
        assert result is True

    @patch("w2v_common.database.connection.get_engine")
    async def test_health_check_unhealthy(self, mock_get_engine):
        from src.database.connection import health_check

        mock_get_engine.side_effect = OSError("down")

        result = await health_check()
        assert result is False


# ============================================================================
# close_engine
# ============================================================================


class TestCloseEngine:
    async def test_close_engine_disposes(self):
        import w2v_common.database.connection as mod

        mock_engine = AsyncMock()
        mod._engine = mock_engine

        await mod.close_engine()
        mock_engine.dispose.assert_awaited_once()
        assert mod._engine is None

    async def test_close_engine_noop_when_none(self):
        import w2v_common.database.connection as mod

        mod._engine = None
        await mod.close_engine()  # Should not raise


# ============================================================================
# _ensure_database_exists -- additional coverage
# ============================================================================


class TestEnsureDatabaseExistsExtended:
    """Cover missing lines: 50, 54, 81-84, 88-91, 96-97."""

    @patch("w2v_common.database.connection.create_engine")
    async def test_psycopg_url_admin_db_replacement(self, mock_create_engine):
        """Verify the admin URL keeps the +psycopg driver and points at the postgres admin DB.

        psycopg v3 is fully sync-capable, so the URL passed to sqlalchemy.create_engine
        retains the postgresql+psycopg:// scheme (no stripping needed). The function
        only swaps the database path from the user's DB to the postgres admin DB.
        """
        from src.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1  # DB exists
        mock_conn.execute.return_value = mock_result

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()
        mock_create_engine.return_value = mock_engine

        with (
            patch("w2v_common.database.connection.DB_NAME", "test_db"),
            patch(
                "w2v_common.database.connection.DATABASE_URL",
                "postgresql+psycopg://user:pass@localhost:5432/test_db",  # pragma: allowlist secret
            ),
        ):
            await _ensure_database_exists(max_retries=1)

        # Verify create_engine was called with the admin URL: same scheme, postgres path
        call_url = mock_create_engine.call_args[0][0]
        assert call_url.startswith("postgresql+psycopg://")
        assert call_url.endswith("/postgres")
        assert "test_db" not in call_url

    @patch("w2v_common.database.connection.create_engine")
    async def test_non_postgres_url_scheme_passthrough(self, mock_create_engine):
        """Test that non-postgresql+psycopg URLs pass through (line 54)."""
        from src.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1
        mock_conn.execute.return_value = mock_result

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()
        mock_create_engine.return_value = mock_engine

        with (
            patch("w2v_common.database.connection.DB_NAME", "test_db"),
            patch(
                "w2v_common.database.connection.DATABASE_URL",
                "postgresql://user:pass@localhost:5432/test_db",  # pragma: allowlist secret
            ),
        ):
            await _ensure_database_exists(max_retries=1)

        call_url = mock_create_engine.call_args[0][0]
        assert call_url.startswith("postgresql://")

    @patch("w2v_common.database.connection.create_engine")
    async def test_create_db_already_exists_programming_error(self, mock_create_engine):
        """Test ProgrammingError 'already exists' during CREATE DATABASE (lines 81-84)."""
        from sqlalchemy.exc import ProgrammingError

        from src.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        check_result = MagicMock()
        check_result.scalar.return_value = None  # DB doesn't exist

        # First call returns check result, second raises ProgrammingError
        mock_conn.execute = MagicMock(
            side_effect=[
                check_result,
                ProgrammingError("", (), Exception("database already exists")),
            ]
        )

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()
        mock_create_engine.return_value = mock_engine

        with patch("w2v_common.database.connection.DB_NAME", "test_db"):
            # Should NOT raise because "already exists" is tolerated
            await _ensure_database_exists(max_retries=1)

    @patch("w2v_common.database.connection.create_engine")
    async def test_create_db_programming_error_other(self, mock_create_engine):
        """Test ProgrammingError that is NOT 'already exists' re-raises (line 84)."""
        from sqlalchemy.exc import ProgrammingError

        from src.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        check_result = MagicMock()
        check_result.scalar.return_value = None  # DB doesn't exist

        mock_conn.execute = MagicMock(
            side_effect=[
                check_result,
                ProgrammingError("", (), Exception("permission denied")),
            ]
        )

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()
        mock_create_engine.return_value = mock_engine

        with patch("w2v_common.database.connection.DB_NAME", "test_db"):
            with pytest.raises(ProgrammingError):
                await _ensure_database_exists(max_retries=1)

    @patch("w2v_common.database.connection.create_engine")
    async def test_operational_error_retries_with_sleep(self, mock_create_engine):
        """Test that OperationalError triggers retry with sleep (lines 88-91)."""
        from sqlalchemy.exc import OperationalError

        from src.database.connection import _ensure_database_exists

        mock_create_engine.side_effect = OperationalError("", (), Exception("refused"))

        with (
            patch("w2v_common.database.connection.DB_NAME", "test_db"),
            patch(
                "w2v_common.database.connection.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await _ensure_database_exists(max_retries=2)

            # Should have slept between retries
            mock_sleep.assert_awaited_once()

    @patch("w2v_common.database.connection.create_engine")
    async def test_unexpected_error_in_ensure_db(self, mock_create_engine):
        """Test unexpected error during _ensure_database_exists (lines 89-91)."""
        from src.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("unexpected")

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()
        mock_create_engine.return_value = mock_engine

        with patch("w2v_common.database.connection.DB_NAME", "test_db"):
            with pytest.raises(RuntimeError, match="unexpected"):
                await _ensure_database_exists(max_retries=1)

    @patch("w2v_common.database.connection.create_engine")
    async def test_engine_dispose_failure_in_finally(self, mock_create_engine):
        """Test engine.dispose() failure in finally block (lines 96-97)."""
        from src.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1
        mock_conn.execute.return_value = mock_result

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock(side_effect=OSError("dispose failed"))
        mock_create_engine.return_value = mock_engine

        with patch("w2v_common.database.connection.DB_NAME", "test_db"):
            # Should not raise -- dispose failure is logged as warning
            await _ensure_database_exists(max_retries=1)


# ============================================================================
# create_engine_with_retry -- coverage for lines 107-165
# ============================================================================


class TestCreateEngineWithRetry:
    """Cover create_engine_with_retry retry paths."""

    @patch("w2v_common.database.connection.create_async_engine")
    async def test_success_first_attempt(self, mock_create_async):
        from src.database.connection import create_engine_with_retry

        mock_conn = AsyncMock()
        mock_engine = AsyncMock()
        mock_engine.begin = MagicMock(return_value=AsyncMock())
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_create_async.return_value = mock_engine

        result = await create_engine_with_retry(max_retries=1)
        assert result is mock_engine

    @patch("w2v_common.database.connection.create_async_engine")
    async def test_db_not_exist_triggers_ensure_then_retries(self, mock_create_async):
        """Test 'database does not exist' triggers _ensure_database_exists (lines 127-146)."""
        from sqlalchemy.exc import OperationalError

        from src.database.connection import create_engine_with_retry

        # First call to create_async_engine raises 'does not exist'
        # Second call (inside ensure path) also fails so it falls to retry
        mock_create_async.side_effect = OperationalError(
            "", (), Exception("database 'test' does not exist")
        )

        with (
            patch("w2v_common.database.connection._ensure_database_exists", new_callable=AsyncMock),
            patch("w2v_common.database.connection.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=1)

    @patch("w2v_common.database.connection.create_async_engine")
    async def test_operational_error_non_db_not_exist(self, mock_create_async):
        """Test OperationalError without 'does not exist' retries (lines 147-151)."""
        from sqlalchemy.exc import OperationalError

        from src.database.connection import create_engine_with_retry

        mock_create_async.side_effect = OperationalError("", (), Exception("connection refused"))

        with patch(
            "w2v_common.database.connection.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)

            mock_sleep.assert_awaited()

    @patch("w2v_common.database.connection.create_async_engine")
    async def test_timeout_error_retries(self, mock_create_async):
        """Test TimeoutError retries (lines 153-157)."""
        from src.database.connection import create_engine_with_retry

        mock_create_async.side_effect = TimeoutError("timed out")

        with patch("w2v_common.database.connection.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)

    @patch("w2v_common.database.connection.create_async_engine")
    async def test_oserror_retries(self, mock_create_async):
        """Test OSError retries (lines 153-157)."""
        from src.database.connection import create_engine_with_retry

        mock_create_async.side_effect = OSError("network unreachable")

        with patch("w2v_common.database.connection.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=1, retry_delay=0.01)

    @patch("w2v_common.database.connection.create_async_engine")
    async def test_generic_exception_retries(self, mock_create_async):
        """Test generic Exception retries (lines 159-163)."""
        from src.database.connection import create_engine_with_retry

        mock_create_async.side_effect = RuntimeError("something unexpected")

        with patch("w2v_common.database.connection.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)

    @patch("w2v_common.database.connection.create_async_engine")
    async def test_db_not_exist_ensure_succeeds_then_connects(self, mock_create_async):
        """Test successful recovery after _ensure_database_exists (lines 130-144)."""
        from sqlalchemy.exc import ProgrammingError

        from src.database.connection import create_engine_with_retry

        mock_conn = AsyncMock()
        mock_good_engine = AsyncMock()
        mock_good_engine.begin = MagicMock(return_value=AsyncMock())
        mock_good_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_good_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)

        # First call fails with "does not exist", second call succeeds
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ProgrammingError("", (), Exception("database 'test' does not exist"))
            return mock_good_engine

        mock_create_async.side_effect = side_effect

        with patch(
            "w2v_common.database.connection._ensure_database_exists", new_callable=AsyncMock
        ):
            result = await create_engine_with_retry(max_retries=3, retry_delay=0.01)
            assert result is mock_good_engine
