"""CGNAT + fail-closed SSRF guard tests for ``src.helpers.url_validation``.

This module's network list was already correct on RFC 6598 CGNAT
(``100.64.0.0/10`` — this estate's Tailscale tailnet, where
``ipaddress.ip_address("100.64.0.5").is_private`` is **False**). These tests
pin that behaviour so it cannot regress, and prove the one defect that was
fixed: DNS failure now fails CLOSED instead of allowing the fetch.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from src.helpers.url_validation import SSRFError, validate_url_safe


def _patch_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """Fixed hostname -> IP mapping; an unmapped host raises ``gaierror``."""

    def fake_getaddrinfo(host: str, *_args: Any, **_kwargs: Any) -> list[Any]:
        ip = mapping.get(host)
        if ip is None:
            raise socket.gaierror(f"unmapped host {host}")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr("src.helpers.url_validation.socket.getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://100.64.0.1/x",
        "http://100.64.0.5/x",
        "http://[::ffff:100.64.0.5]/x",
    ],
)
def test_cgnat_literals_rejected(bad_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(
        monkeypatch,
        {
            "100.64.0.1": "100.64.0.1",
            "100.64.0.5": "100.64.0.5",
            "::ffff:100.64.0.5": "::ffff:100.64.0.5",
        },
    )
    with pytest.raises(SSRFError, match="blocked address"):
        validate_url_safe(bad_url)


def test_hostname_resolving_to_cgnat_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(monkeypatch, {"artifact-host.example.com": "100.64.0.5"})
    with pytest.raises(SSRFError, match="blocked address"):
        validate_url_safe("https://artifact-host.example.com/app.apk")


def test_public_host_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(monkeypatch, {"example.com": "93.184.216.34"})
    validate_url_safe("https://example.com/v.mp4")


@pytest.mark.parametrize("ip", ["100.63.255.255", "100.128.0.0"])
def test_cgnat_boundary_addresses_still_accepted(ip: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deny is the /10, not ``startswith("100.")`` — both neighbours pass."""
    _patch_dns(monkeypatch, {ip: ip})
    validate_url_safe(f"http://{ip}/x")


def test_dns_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixed defect: ``socket.gaierror`` used to ``return`` (fail-open)."""
    _patch_dns(monkeypatch, {})
    with pytest.raises(SSRFError, match="DNS resolution failed"):
        validate_url_safe("http://unresolvable.example.com/app.apk")
