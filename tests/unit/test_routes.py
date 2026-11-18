"""Unit tests for API route helpers and background processing."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from w2v_common.tenant import InvalidTenantIdError, MissingTenantError

from src.api.routes._helpers import _build_screen_info, _get_tenant_id
from src.tenant import require_tenant
from tests.conftest import NIL_TENANT_SPELLINGS, TENANT_APP

# ============================================================================
# _get_tenant_id
# ============================================================================


class TestGetTenantId:
    def test_returns_tenant_from_state(self):
        request = MagicMock()
        request.state.tenant_id = TENANT_APP
        assert _get_tenant_id(request) == TENANT_APP

    def test_undashed_spelling_is_canonicalised(self):
        """Two spellings of one tenant must not become two DB/redis keys."""
        request = MagicMock()
        request.state.tenant_id = TENANT_APP.replace("-", "")
        assert _get_tenant_id(request) == TENANT_APP

    def test_absent_tenant_is_refused_not_substituted(self):
        """This used to return the literal ``"anonymous"``.

        That sentinel was a shared bucket every unauthenticated caller landed
        in, and it is the exact substitution w2v-common 0.67.0 deleted. An
        absent tenant is now a 401, not a default.
        """
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            _get_tenant_id(request)
        assert exc_info.value.status_code == 401
        assert "anonymous" not in str(exc_info.value.detail)

    def test_blank_tenant_is_refused(self):
        request = MagicMock()
        request.state.tenant_id = "   "
        with pytest.raises(HTTPException) as exc_info:
            _get_tenant_id(request)
        assert exc_info.value.status_code == 401

    def test_slug_tenant_is_refused(self):
        """A legacy slug must not become a second, un-owned tenant key."""
        request = MagicMock()
        request.state.tenant_id = "my-tenant"
        with pytest.raises(HTTPException) as exc_info:
            _get_tenant_id(request)
        assert exc_info.value.status_code == 401
        # Never echo the value: X-Tenant-Id can carry `Bearer <license token>`.
        assert "my-tenant" not in str(exc_info.value.detail)

    @pytest.mark.parametrize("nil_spelling", NIL_TENANT_SPELLINGS)
    def test_nil_uuid_is_refused_in_every_spelling(self, nil_spelling):
        """The nil uuid is refused BY VALUE, not by comparing one literal.

        ``uuid.UUID`` accepts dashed, undashed, braced and ``urn:uuid:``
        spellings of the all-zeros value. The retired fail-safe wrote the
        dashed one, so a string compare against that literal would let the
        other three through into a real, mis-attributed tenant column.
        """
        request = MagicMock()
        request.state.tenant_id = nil_spelling
        with pytest.raises(HTTPException) as exc_info:
            _get_tenant_id(request)
        assert exc_info.value.status_code == 401


class TestRequireTenant:
    """``require_tenant`` is the coercion point behind every boundary."""

    def test_returns_a_real_uuid(self):
        assert require_tenant(TENANT_APP) == uuid.UUID(TENANT_APP)

    def test_missing_raises(self):
        with pytest.raises(MissingTenantError):
            require_tenant(None)

    def test_slug_raises(self):
        with pytest.raises(InvalidTenantIdError):
            require_tenant("tenant-1")

    @pytest.mark.parametrize("nil_spelling", NIL_TENANT_SPELLINGS)
    def test_nil_uuid_raises_in_every_spelling(self, nil_spelling):
        with pytest.raises(InvalidTenantIdError):
            require_tenant(nil_spelling)


# ============================================================================
# _build_screen_info
# ============================================================================


class TestBuildScreenInfo:
    def test_minimal_screen_dict(self):
        screen_dict = {"screen_id": "s0"}
        info = _build_screen_info(screen_dict)
        assert info.screen_id == "s0"
        assert info.screen_type == "unknown"
        assert info.interactive_elements == 0
        assert info.accessibility_ids == []
        assert info.headings == []
        assert info.has_forms is False
        assert info.has_media is False
        assert info.content_sections == []
        assert info.accessibility_issues == []
        assert info.accessibility_score is None  # absent != a perfect score
        assert info.interactive_elements_detail == []

    def test_full_screen_dict(self):
        screen_dict = {
            "screen_id": "s1",
            "screen_type": "login",
            "activity_name": "com.app.LoginActivity",
            "text_content": "Sign in",
            "interactive_elements": 3,
            "accessibility_ids": ["btn_login"],
            "screenshot_path": "/tmp/screen.png",
            "headings": ["Welcome"],
            "has_forms": True,
            "has_media": False,
            "content_sections": [{"heading": "Login", "element_count": 2, "has_interactive": True}],
            "accessibility_issues": [
                {
                    "rule": "missing_label",
                    "severity": "error",
                    "element_tag": "EditText",
                    "element_text": "",
                    "message": "Missing label",
                }
            ],
            "accessibility_score": 75,
            "interactive_elements_detail": [
                {
                    "tag": "Button",
                    "text": "Login",
                    "content_desc": "",
                    "resource_id": "btn_login",
                    "clickable": True,
                }
            ],
        }
        info = _build_screen_info(screen_dict)
        assert info.screen_id == "s1"
        assert info.screen_type == "login"
        assert info.activity_name == "com.app.LoginActivity"
        assert info.interactive_elements == 3
        assert len(info.content_sections) == 1
        assert len(info.accessibility_issues) == 1
        assert info.accessibility_score == 75
        assert len(info.interactive_elements_detail) == 1


# ============================================================================
# _process_inspection_bg
# ============================================================================


class TestProcessInspectionBg:
    @patch("src.api.routes.inspections.get_db_session")
    async def test_background_calls_process_job(self, mock_get_db_session):
        from src.api.routes.inspections import _process_inspection_bg

        mock_db = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_db)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_get_db_session.return_value = ctx

        service = AsyncMock()
        service.process_job = AsyncMock()

        await _process_inspection_bg(service, "job-1", TENANT_APP)

        service.process_job.assert_awaited_once_with(
            mock_db, "job-1", TENANT_APP, farm_credentials=None
        )

    @patch("src.api.routes.inspections.JobManager")
    @patch("src.api.routes.inspections.get_db_session")
    async def test_background_error_logged(self, mock_get_db_session, mock_jm):
        from src.api.routes.inspections import _process_inspection_bg

        mock_db = AsyncMock()
        mock_db2 = AsyncMock()

        # get_db_session is called twice: once for process_job, once for mark_failed
        ctx1 = AsyncMock()
        ctx1.__aenter__ = AsyncMock(return_value=mock_db)
        ctx1.__aexit__ = AsyncMock(return_value=False)

        ctx2 = AsyncMock()
        ctx2.__aenter__ = AsyncMock(return_value=mock_db2)
        ctx2.__aexit__ = AsyncMock(return_value=False)

        mock_get_db_session.side_effect = [ctx1, ctx2]

        mock_jm.mark_failed = AsyncMock()

        service = AsyncMock()
        service.process_job = AsyncMock(side_effect=OSError("boom"))

        # Should not raise
        await _process_inspection_bg(service, "job-1", TENANT_APP)

        # Should have attempted to mark failed in a separate session
        mock_jm.mark_failed.assert_awaited_once_with(mock_db2, "job-1", "boom")

    @patch("src.api.routes.inspections.get_db_session")
    async def test_background_refuses_a_non_uuid_tenant(self, mock_get_db_session):
        """A background job with no real tenant must die loudly, not run.

        It writes rows and binds ``app.current_tenant``; running it under a slug
        or the nil uuid is how work gets mis-attributed. ``require_tenant``
        raises BEFORE any session is opened.
        """
        from src.api.routes.inspections import _process_inspection_bg

        service = AsyncMock()
        with pytest.raises(InvalidTenantIdError):
            await _process_inspection_bg(service, "job-1", "tenant-1")
        mock_get_db_session.assert_not_called()
        service.process_job.assert_not_awaited()

    @pytest.mark.parametrize("nil_spelling", NIL_TENANT_SPELLINGS)
    @patch("src.api.routes.inspections.get_db_session")
    async def test_background_refuses_the_nil_tenant(self, mock_get_db_session, nil_spelling):
        from src.api.routes.inspections import _process_inspection_bg

        service = AsyncMock()
        with pytest.raises(InvalidTenantIdError):
            await _process_inspection_bg(service, "job-1", nil_spelling)
        mock_get_db_session.assert_not_called()
        service.process_job.assert_not_awaited()
