"""Unit tests for src.utils.redaction."""

from __future__ import annotations

import pytest

from src.utils.redaction import redact_url_credentials


class TestRedactUrlCredentials:
    def test_masks_user_and_password(self):
        url = "https://bob:hunter2@hub-cloud.browserstack.com/wd/hub"  # pragma: allowlist secret
        assert redact_url_credentials(url) == "https://***:***@hub-cloud.browserstack.com/wd/hub"

    def test_masks_username_only_userinfo(self):
        assert redact_url_credentials("https://bob@example.com/x") == "https://***@example.com/x"

    def test_preserves_path_query_and_fragment(self):
        url = "https://u:p@example.com/wd/hub?a=1#frag"  # pragma: allowlist secret
        assert redact_url_credentials(url) == "https://***:***@example.com/wd/hub?a=1#frag"

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:4723",
            "https://hub-cloud.browserstack.com/wd/hub",
            "",
            "not a url at all",
            "/relative/path",
            "wd/hub",
        ],
    )
    def test_urls_without_userinfo_pass_through_unchanged(self, url):
        assert redact_url_credentials(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "user:pw@hub-cloud.browserstack.com/wd/hub",  # pragma: allowlist secret
            "bsuser:accesskey123@hub-cloud.browserstack.com/wd/hub",  # pragma: allowlist secret
            "user@host/wd/hub",
        ],
    )
    def test_schemeless_userinfo_is_masked(self, url):
        """A schemeless override lands entirely in urlsplit's PATH.

        ``appium_url`` is a caller-supplied override (src/api/schemas.py), so a
        hand-written value can skip the ``https://`` that ``get_appium_url``
        always emits. With no ``//`` there is no netloc to mask, and the old
        implementation returned the credential verbatim.
        """
        assert redact_url_credentials(url) == "***"

    def test_at_in_path_query_fragment_is_not_masked(self):
        """A real authority is present, so ``@`` further right is not userinfo."""
        for url in (
            "https://example.com/pa@th",
            "https://example.com/p?q=a@b",
            "https://example.com/p#fr@g",
        ):
            assert redact_url_credentials(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://[::1",
            "http://u:p@[not-an-ipv6/wd/hub",  # pragma: allowlist secret
            "http://u:p@host:notaport/wd/hub",  # pragma: allowlist secret
            "://@@@",
        ],
    )
    def test_malformed_urls_never_raise(self, url):
        result = redact_url_credentials(url)
        assert isinstance(result, str)
        assert "p@" not in result or result == "***"
