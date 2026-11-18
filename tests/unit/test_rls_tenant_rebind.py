"""The ``after_begin`` GUC rebind must actually reach postgres.

``install_rls_tenant_rebind`` had NO test. That matters more since w2v-common
0.67.0: ``get_tenant_id_from_context()`` now returns a ``uuid.UUID`` where it
used to return a ``str``, and psycopg adapts a ``UUID`` object to the postgres
``uuid`` type — so ``set_config(text, uuid, boolean)`` resolves to no function
and the rebind raises instead of binding. Nothing in the suite would have
noticed: the listener is registered globally, swallows nothing, and the failure
only surfaces on the SECOND transaction of a background job.

These tests drive a REAL session against the CI postgres and read the GUC back.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from w2v_common.auth.identity_context import clear_identity_context, set_identity_context

from src.database.rls_events import install_rls_tenant_rebind
from tests.conftest import TENANT_APP


@pytest.fixture
def _rls_enforce(monkeypatch):
    monkeypatch.setenv("TENANT_RLS_MODE", "enforce")
    install_rls_tenant_rebind()
    yield
    clear_identity_context()


async def _current_tenant(session) -> str | None:
    """Return the GUC, or ``None`` — ``current_setting(..., true)`` yields NULL
    when the setting was never set in this transaction."""
    result = await session.execute(text("SELECT current_setting('app.current_tenant', true)"))
    return result.scalar_one()


async def test_rebind_binds_the_tenant_guc(db_session_factory, _rls_enforce) -> None:
    """A uuid tenant in context must land in ``app.current_tenant`` as text."""
    set_identity_context(uuid.UUID(TENANT_APP), None, None)
    async with db_session_factory() as session:
        await session.execute(text("SELECT 1"))  # opens the transaction
        assert await _current_tenant(session) == TENANT_APP


async def test_rebind_survives_a_commit(db_session_factory, _rls_enforce) -> None:
    """The GUC is transaction-scoped, so the transaction AFTER a commit must re-bind.

    This is the whole reason the listener exists: the background inspection
    pipeline commits several times on one session, and every write after the
    first commit would fail closed to 0 rows without the rebind.
    """
    set_identity_context(uuid.UUID(TENANT_APP), None, None)
    async with db_session_factory() as session:
        await session.execute(text("SELECT 1"))
        await session.commit()
        # Fresh transaction — the GUC set with is_local=true is gone unless
        # after_begin re-applied it.
        assert await _current_tenant(session) == TENANT_APP


async def test_no_tenant_in_context_binds_nothing(db_session_factory, _rls_enforce) -> None:
    """Startup/maintenance contexts have no tenant; the listener must be inert."""
    clear_identity_context()
    async with db_session_factory() as session:
        await session.execute(text("SELECT 1"))
        assert await _current_tenant(session) in (None, "")


async def test_rebind_is_off_when_mode_is_off(db_session_factory, monkeypatch) -> None:
    monkeypatch.setenv("TENANT_RLS_MODE", "off")
    install_rls_tenant_rebind()
    set_identity_context(uuid.UUID(TENANT_APP), None, None)
    try:
        async with db_session_factory() as session:
            await session.execute(text("SELECT 1"))
            assert await _current_tenant(session) in (None, "")
    finally:
        clear_identity_context()
