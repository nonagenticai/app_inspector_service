"""Unit tests for URL validation and SSRF protection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.helpers.url_validation import SSRFError, _is_blocked_ip, validate_url_safe


class TestIsBlockedIp:
    def test_loopback_blocked(self):
        assert _is_blocked_ip("127.0.0.1") is True

    def test_private_10_blocked(self):
        assert _is_blocked_ip("10.0.0.1") is True

    def test_private_172_blocked(self):
        assert _is_blocked_ip("172.16.0.1") is True

    def test_private_192_blocked(self):
        assert _is_blocked_ip("192.168.1.1") is True

    def test_link_local_blocked(self):
        assert _is_blocked_ip("169.254.1.1") is True

    def test_public_ip_allowed(self):
        assert _is_blocked_ip("8.8.8.8") is False

    def test_ipv6_loopback_blocked(self):
        assert _is_blocked_ip("::1") is True

    def test_invalid_ip_not_blocked(self):
        assert _is_blocked_ip("not-an-ip") is False

    def test_ipv6_unique_local_blocked(self):
        assert _is_blocked_ip("fc00::1") is True

    def test_ipv4_mapped_ipv6_blocked(self):
        # ::ffff:127.0.0.1
        assert _is_blocked_ip("::ffff:127.0.0.1") is True

    def test_ipv4_mapped_ipv6_public_allowed(self):
        assert _is_blocked_ip("::ffff:8.8.8.8") is False


class TestValidateUrlSafe:
    def test_no_hostname_raises_valueerror(self):
        with pytest.raises(ValueError, match="Cannot extract hostname"):
            validate_url_safe("not-a-url")

    def test_blocked_hostname_raises_ssrf(self):
        with pytest.raises(SSRFError, match="cloud metadata"):
            validate_url_safe("http://metadata.google.internal/computeMetadata/v1")

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_private_ip_raises_ssrf(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [(2, 1, 6, "", ("192.168.1.1", 80))]
        with pytest.raises(SSRFError, match="blocked address"):
            validate_url_safe("http://internal-service.local:8080")

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_public_ip_allowed(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        # Should not raise
        validate_url_safe("http://example.com")

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_dns_failure_rejected_fail_closed(self, mock_getaddrinfo):
        """A host we cannot resolve must NOT be handed to httpx to resolve itself.

        This used to ``return`` (fail-open), leaving the DNS-rebinding TOCTOU
        window open. It now matches ``w2v_common.net.validate_public_url``.
        """
        import socket

        mock_getaddrinfo.side_effect = socket.gaierror("DNS failed")
        with pytest.raises(SSRFError, match="DNS resolution failed"):
            validate_url_safe("http://unknown-host.example.com")
