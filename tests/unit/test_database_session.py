"""Unit tests for database session management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================================
# get_session_maker
# ============================================================================


class TestGetSessionMaker:
    @patch("w2v_common.database.session.get_engine")
    async def test_creates_session_maker(self, mock_get_engine):
        import w2v_common.database.session as mod

        mod._async_session_maker = None

        mock_engine = AsyncMock()
        mock_get_engine.return_value = mock_engine

        with patch("w2v_common.database.session.async_sessionmaker") as mock_sessionmaker:
            mock_sm = MagicMock()
            mock_sessionmaker.return_value = mock_sm

            result = await mod.get_session_maker()

        assert result is mock_sm
        mock_sessionmaker.assert_called_once()
        mod._async_session_maker = None

    @patch("w2v_common.database.session.get_engine")
    async def test_caches_session_maker(self, mock_get_engine):
        import w2v_common.database.session as mod

        existing_sm = MagicMock()
        mod._async_session_maker = existing_sm

        result = await mod.get_session_maker()
        assert result is existing_sm
        mock_get_engine.assert_not_awaited()
        mod._async_session_maker = None


# ============================================================================
# get_db (FastAPI dependency)
# ============================================================================


class TestGetDb:
    @patch("w2v_common.database.session.get_session_maker")
    async def test_get_db_yields_session_and_commits(self, mock_gsm):
        from src.database.session import get_db

        mock_session = AsyncMock()
        mock_sm = MagicMock()
        mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_gsm.return_value = mock_sm

        gen = get_db()
        session = await gen.__anext__()
        assert session is mock_session

        # Complete the generator normally
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    @patch("w2v_common.database.session.get_session_maker")
    async def test_get_db_rolls_back_on_error(self, mock_gsm):
        from src.database.session import get_db

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock(side_effect=OSError("commit failed"))
        mock_sm = MagicMock()
        mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_gsm.return_value = mock_sm

        gen = get_db()
        session = await gen.__anext__()
        assert session is mock_session

        with pytest.raises(Exception, match="commit failed"):
            await gen.__anext__()


# ============================================================================
# get_db_session (context manager)
# ============================================================================


class TestGetDbSession:
    @patch("w2v_common.database.session.get_session_maker")
    async def test_get_db_session_commits(self, mock_gsm):
        from src.database.session import get_db_session

        mock_session = AsyncMock()
        mock_sm = MagicMock()
        mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_gsm.return_value = mock_sm

        async with get_db_session() as session:
            assert session is mock_session

    @patch("w2v_common.database.session.get_session_maker")
    async def test_get_db_session_rolls_back_on_error(self, mock_gsm):
        from src.database.session import get_db_session

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock(side_effect=OSError("fail"))
        mock_sm = MagicMock()
        mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_gsm.return_value = mock_sm

        with pytest.raises(Exception, match="fail"):
            async with get_db_session() as _session:
                pass


# ============================================================================
# close_session_maker
# ============================================================================


class TestCloseSessionMaker:
    async def test_close_resets_to_none(self):
        import w2v_common.database.session as mod

        mod._async_session_maker = MagicMock()
        await mod.close_session_maker()
        assert mod._async_session_maker is None
