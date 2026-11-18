"""Unit tests for auth middleware -- trusted networks, token extraction, dispatch."""

from __future__ import annotations

import importlib
import importlib.util
import ipaddress
import json
import uuid
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import w2v_common.auth.middleware as _w2v_mw
from pydantic import ValidationError
from w2v_common.tenant import MissingTenantError

from src.auth.middleware import (
    PUBLIC_ENDPOINTS,
    PUBLIC_PREFIXES,
    AuthMiddleware,
    _introspect_license,
    _is_trusted_network,
)
from src.auth.models import User
from tests.conftest import TENANT_APP

# ============================================================================
# Trust lanes are explicit opt-in (w2v_common >= 0.89.0)
# ============================================================================

# w2v_common 0.89.0 changed the TRUSTED_CIDRS default to EMPTY: no source IP is
# trusted unless the operator names the network. Tests that exercise the
# trusted-network code path therefore set TRUSTED_CIDRS explicitly (to the
# loopback + cluster-private ranges these tests were written against).
#
# The CIDRs are parsed by executing a FRESH, isolated copy of
# w2v_common.auth.middleware with TRUSTED_CIDRS in the env -- the real
# import-time parse -- and the result is monkeypatched onto the live module.
# ``importlib.reload`` on the live module is deliberately avoided: it rebinds
# AuthMiddleware & co. to new class objects, which breaks identity checks in
# other tests (test_auth_middleware_wiring asserts ``AuthMiddleware is
# SharedAuthMiddleware``).
EXPLICIT_TRUSTED_CIDRS = "127.0.0.1/32,::1/128,172.16.0.0/12,10.0.0.0/8"


def _fresh_middleware(monkeypatch: pytest.MonkeyPatch, value: str | None) -> ModuleType:
    """Execute an isolated copy of the middleware with TRUSTED_CIDRS set (or unset)."""
    if value is None:
        monkeypatch.delenv("TRUSTED_CIDRS", raising=False)
    else:
        monkeypatch.setenv("TRUSTED_CIDRS", value)
    spec = importlib.util.find_spec(_w2v_mw.__name__)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def trusted_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust exactly EXPLICIT_TRUSTED_CIDRS on the live middleware for this test."""
    fresh = _fresh_middleware(monkeypatch, EXPLICIT_TRUSTED_CIDRS)
    monkeypatch.setattr(_w2v_mw, "TRUSTED_NETWORKS", fresh.TRUSTED_NETWORKS)


# ============================================================================
# _is_trusted_network
# ============================================================================


@pytest.mark.usefixtures("trusted_cidrs")
class TestIsTrustedNetwork:
    def test_loopback_trusted(self):
        assert _is_trusted_network("127.0.0.1") is True

    def test_ipv6_loopback_trusted(self):
        assert _is_trusted_network("::1") is True

    def test_private_192_168_not_trusted_by_default(self):
        # 192.168.0.0/16 was deliberately removed from the default TRUSTED_CIDRS
        # in w2v_common to fix the SSR-proxy source-IP bypass (see
        # project_orchestrator_trusted_networks_bypass_2026-04-18). Cluster pod
        # IPs and node IPs live in 10.0.0.0/8 / 172.16.0.0/12.
        assert _is_trusted_network("192.168.1.100") is False

    def test_private_10_network(self):
        assert _is_trusted_network("10.0.0.5") is True

    def test_private_172_16(self):
        assert _is_trusted_network("172.16.0.1") is True

    def test_public_ip_not_trusted(self):
        assert _is_trusted_network("8.8.8.8") is False

    def test_none_not_trusted(self):
        assert _is_trusted_network(None) is False

    def test_empty_string_not_trusted(self):
        assert _is_trusted_network("") is False

    def test_invalid_ip_not_trusted(self):
        assert _is_trusted_network("not-an-ip") is False


# ============================================================================
# PUBLIC_ENDPOINTS and PUBLIC_PREFIXES
# ============================================================================


class TestPublicPaths:
    def test_health_is_public(self):
        assert "/health" in PUBLIC_ENDPOINTS

    def test_healthz_is_public(self):
        assert "/healthz" in PUBLIC_ENDPOINTS

    def test_docs_is_public(self):
        assert "/docs" in PUBLIC_ENDPOINTS

    def test_health_prefix(self):
        assert "/health/" in PUBLIC_PREFIXES

    def test_trusted_networks_parsed(self, monkeypatch: pytest.MonkeyPatch):
        mod = _fresh_middleware(monkeypatch, EXPLICIT_TRUSTED_CIDRS)
        assert [
            ipaddress.ip_network(c) for c in EXPLICIT_TRUSTED_CIDRS.split(",")
        ] == mod.TRUSTED_NETWORKS

    def test_trusted_networks_empty_by_default(self, monkeypatch: pytest.MonkeyPatch):
        """w2v_common >= 0.89.0: with TRUSTED_CIDRS unset, nothing is trusted."""
        mod = _fresh_middleware(monkeypatch, None)
        assert mod.TRUSTED_NETWORKS == []
        assert mod._is_trusted_network("127.0.0.1") is False
        assert mod._is_trusted_network("10.0.0.5") is False


# ============================================================================
# _introspect_license
# ============================================================================


class TestIntrospectLicense:
    @patch("w2v_common.auth.middleware.httpx.AsyncClient")
    async def test_active_license_cached(self, mock_client_cls):
        from w2v_common.auth.middleware import _get_license_cache

        _get_license_cache().clear()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True, "tenant_id": "t1"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await _introspect_license("valid-token-123")
        assert result is not None
        assert result["active"] is True
        assert result["tenant_id"] == "t1"

        _get_license_cache().clear()

    @patch("w2v_common.auth.middleware.httpx.AsyncClient")
    async def test_inactive_license(self, mock_client_cls):
        from w2v_common.auth.middleware import _get_license_cache

        _get_license_cache().clear()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": False, "reason": "expired"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await _introspect_license("expired-token")
        assert result is not None
        assert result["active"] is False

        _get_license_cache().clear()

    @patch("w2v_common.auth.middleware.httpx.AsyncClient")
    async def test_introspection_error_returns_none(self, mock_client_cls):
        from w2v_common.auth.middleware import _get_license_cache

        _get_license_cache().clear()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("network error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await _introspect_license("any-token")
        assert result is None

        _get_license_cache().clear()


# ============================================================================
# Token extraction
# ============================================================================


class TestTokenExtraction:
    def _make_request(self, headers: dict | None = None):
        request = MagicMock()
        request.headers = headers or {}
        return request

    def test_authorization_bearer_is_license_token_when_not_jwt_shaped(self):
        """Per w2v_common 0.74.0, a non-JWT-shaped `Authorization: Bearer` is the
        PLATFORM license-token convention and IS read as a license token (only a
        JWT-shaped bearer is reserved for upstream Keycloak auth and excluded --
        see test_authorization_jwt_bearer_is_not_a_license_token below)."""
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"Authorization": "Bearer my-token"})
        assert mw._extract_license_token(request) == "my-token"
        assert mw._extract_jwt_bearer(request) == "my-token"

    def test_authorization_jwt_bearer_is_not_a_license_token(self):
        """A JWT-shaped Authorization Bearer (three dot-separated segments) is
        never read as a license token -- it is candidate input for Keycloak JWT
        validation only (the 2026-04-26 prod 401s on /api/assets/procedures and
        /api/assets/secrets came from a JWT reaching license introspection)."""
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"Authorization": "Bearer aaa.bbb.ccc"})
        assert mw._extract_license_token(request) is None
        assert mw._extract_jwt_bearer(request) == "aaa.bbb.ccc"

    def test_bearer_in_x_tenant_id(self):
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"X-Tenant-Id": "Bearer license-token"})
        assert mw._extract_license_token(request) == "license-token"

    def test_no_token_found(self):
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({})
        assert mw._extract_license_token(request) is None
        assert mw._extract_jwt_bearer(request) is None

    def test_non_bearer_x_tenant_id_ignored(self):
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"X-Tenant-Id": "plain-tenant-id"})
        assert mw._extract_license_token(request) is None

    def test_x_tenant_id_is_sole_license_source(self):
        """X-Tenant-Id is checked FIRST and wins when both headers are present,
        even though (per w2v_common 0.74.0) a non-JWT-shaped Authorization Bearer
        is also an accepted license-token shape on its own -- see
        test_authorization_bearer_is_license_token_when_not_jwt_shaped."""
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request(
            {"X-Tenant-Id": "Bearer token-a", "Authorization": "Bearer token-b"}
        )
        assert mw._extract_license_token(request) == "token-a"
        assert mw._extract_jwt_bearer(request) == "token-b"


# ============================================================================
# AuthMiddleware.dispatch
# ============================================================================


class TestAuthMiddlewareDispatch:
    async def test_public_endpoint_bypasses_auth(self):
        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/health"
        expected_response = MagicMock()
        call_next = AsyncMock(return_value=expected_response)

        response = await mw.dispatch(request, call_next)
        assert response is expected_response

    async def test_public_prefix_bypasses_auth(self):
        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/health/ready"
        expected_response = MagicMock()
        call_next = AsyncMock(return_value=expected_response)

        response = await mw.dispatch(request, call_next)
        assert response is expected_response

    @patch("w2v_common.auth.middleware._introspect_license")
    async def test_valid_token_sets_tenant(self, mock_introspect):
        mock_introspect.return_value = {"active": True, "tenant_id": TENANT_APP}

        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/inspect"
        request.headers = {"X-Tenant-Id": "Bearer valid-token"}
        request.state = MagicMock()
        expected_response = MagicMock()
        call_next = AsyncMock(return_value=expected_response)

        response = await mw.dispatch(request, call_next)
        assert response is expected_response
        assert request.state.tenant_id == TENANT_APP

    @patch("w2v_common.auth.middleware._introspect_license")
    async def test_active_license_with_slug_tenant_returns_401(self, mock_introspect):
        """``active: true`` is not enough — the tenant must be a real uuid.

        w2v-common 0.67.0 refuses a slug (or empty) tenant on an otherwise
        valid introspection payload. Before that, a slug propagated into
        ``request.state.tenant_id`` and every downstream tenant predicate
        compared against a value no row could ever legitimately carry, which
        silently bypassed isolation rather than failing.
        """
        mock_introspect.return_value = {"active": True, "tenant_id": "tenant-1"}

        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/inspect"
        request.headers = {"X-Tenant-Id": "Bearer valid-token"}
        request.state = MagicMock()
        call_next = AsyncMock()

        response = await mw.dispatch(request, call_next)
        call_next.assert_not_called()
        assert response.status_code == 401

    @patch("w2v_common.auth.middleware._introspect_license")
    async def test_invalid_token_returns_401(self, mock_introspect):
        mock_introspect.return_value = {"active": False, "reason": "expired"}

        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/inspect"
        request.headers = {"X-Tenant-Id": "Bearer bad-token"}
        call_next = AsyncMock()

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401

    @patch("w2v_common.auth.middleware._introspect_license")
    async def test_none_introspect_returns_401(self, mock_introspect):
        mock_introspect.return_value = None

        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/inspect"
        request.headers = {"X-Tenant-Id": "Bearer some-token"}
        call_next = AsyncMock()

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401

    @pytest.mark.usefixtures("trusted_cidrs")
    async def test_trusted_network_no_token_returns_401(self):
        """w2v_common 0.6.9 flips AUTH_TRUST_INCLUSTER_IDENTITY default to false.

        Trusted-network identity inference is fail-closed by default. A request
        from a trusted CIDR with neither a license token nor a JWT must now
        return 401 instead of falling through to a default tenant.
        """
        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/inspect"
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        request.state = MagicMock()
        call_next = AsyncMock()

        response = await mw.dispatch(request, call_next)
        call_next.assert_not_called()
        assert response.status_code == 401

    @pytest.mark.usefixtures("trusted_cidrs")
    async def test_trusted_network_with_plain_x_tenant_id_returns_401(self):
        """Bare `X-Tenant-Id: <id>` (no Bearer) is no longer a valid auth path.

        With AUTH_TRUST_INCLUSTER_IDENTITY=false (w2v_common 0.6.9 default),
        only `X-Tenant-Id: Bearer <license-token>` authenticates a request.
        """
        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/inspect"
        request.headers = {"X-Tenant-Id": "custom-tenant"}
        request.client = MagicMock()
        request.client.host = "10.0.0.1"
        request.state = MagicMock()
        call_next = AsyncMock()

        response = await mw.dispatch(request, call_next)
        call_next.assert_not_called()
        assert response.status_code == 401

    async def test_untrusted_no_token_returns_401(self):
        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/inspect"
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "8.8.8.8"
        call_next = AsyncMock()

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401

    async def test_no_client_returns_401(self):
        mw = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/inspect"
        request.headers = {}
        request.client = None
        call_next = AsyncMock()

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401


# ============================================================================
# User model
# ============================================================================


class TestUserModel:
    def test_effective_tenant_id_from_tenant(self):
        user = User(sub="user-1", tenant_id=uuid.UUID(TENANT_APP))
        assert user.effective_tenant_id == uuid.UUID(TENANT_APP)

    def test_slug_tenant_is_rejected_by_the_model(self):
        """``User.tenant_id`` is a ``uuid.UUID | None`` since w2v-common 0.67.0.

        Built by actually decoding JSON rather than written as a literal: the
        value this guards against arrives from an UNTYPED source (the
        licenses-service introspection payload), so the static type says
        nothing and pydantic is the only gate. A literal here would just be a
        ty error, and the runtime check could never be written at all.
        """
        slug_from_untyped_json = json.loads('"tenant-1"')
        with pytest.raises(ValidationError):
            User(sub="user-1", tenant_id=slug_from_untyped_json)

    def test_absent_tenant_raises_instead_of_falling_back_to_sub(self):
        """This used to return ``sub`` — a per-USER tenant, i.e. no tenant at all.

        A subject id silently standing in for a tenant meant every row that
        user wrote was scoped to a tenant that does not exist in licenses.
        0.67.0 made it raise; assert the REFUSAL, not the old substitution.
        """
        user = User(sub="user-1", tenant_id=None)
        with pytest.raises(MissingTenantError):
            _ = user.effective_tenant_id
