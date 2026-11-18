"""URL validation -- SSRF protection for outbound HTTP requests in app_inspector_service."""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse, urlunparse

from src.utils.redaction import redact_url_credentials

logger = logging.getLogger(__name__)


class SSRFError(ValueError):
    """Raised when a URL targets a blocked network range."""


# Private/reserved IPv4 networks that must never be accessed by the service.
_BLOCKED_IPV4_NETWORKS = [
    ipaddress.IPv4Network("127.0.0.0/8"),  # Loopback
    ipaddress.IPv4Network("10.0.0.0/8"),  # Private class A
    ipaddress.IPv4Network("172.16.0.0/12"),  # Private class B
    ipaddress.IPv4Network("192.168.0.0/16"),  # Private class C
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-local
    ipaddress.IPv4Network("100.64.0.0/10"),  # CGNAT (RFC 6598)
    ipaddress.IPv4Network("0.0.0.0/8"),  # "This" network
    ipaddress.IPv4Network("224.0.0.0/4"),  # Multicast
    ipaddress.IPv4Network("240.0.0.0/4"),  # Reserved (future use)
    ipaddress.IPv4Network("255.255.255.255/32"),  # Broadcast
]

_BLOCKED_IPV6_NETWORKS = [
    ipaddress.IPv6Network("::1/128"),  # Loopback
    ipaddress.IPv6Network("fc00::/7"),  # Unique local
    ipaddress.IPv6Network("fe80::/10"),  # Link-local
    ipaddress.IPv6Network("ff00::/8"),  # Multicast
    ipaddress.IPv6Network("::ffff:0:0/96"),  # IPv4-mapped (checked explicitly too)
]

# Hostnames known to expose cloud metadata services.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
    }
)


def _is_blocked_ip(addr: str) -> bool:
    """Check if an IP address string falls within a blocked range."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _BLOCKED_IPV4_NETWORKS)
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return any(mapped in net for net in _BLOCKED_IPV4_NETWORKS)
        sixtofour = ip.sixtofour
        if sixtofour is not None:
            return any(sixtofour in net for net in _BLOCKED_IPV4_NETWORKS)
        teredo = ip.teredo
        if teredo is not None:
            _server, client = teredo
            return any(client in net for net in _BLOCKED_IPV4_NETWORKS)
        return any(ip in net for net in _BLOCKED_IPV6_NETWORKS)
    return False


def _resolve_and_check(url: str) -> list[str]:
    """Resolve ``url``'s hostname and reject any blocked address, returning the
    list of resolved (all-public) IP strings in ``getaddrinfo`` order.

    Raises:
        ValueError: If hostname cannot be extracted.
        SSRFError: If the URL resolves to a blocked address or hostname, or if
            DNS resolution fails (fail-closed — see below).
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Cannot extract hostname from URL: {redact_url_credentials(url)}")

    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise SSRFError(f"Blocked hostname: {hostname} (cloud metadata service)")

    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        # Fail-CLOSED, matching w2v_common.net.validate_public_url. Returning
        # here used to hand an unresolvable host straight to httpx, which then
        # resolved it independently — the DNS-rebinding TOCTOU window, and the
        # guard's only remaining fail-open.
        logger.warning("DNS resolution failed for %s, rejecting URL (fail-closed)", hostname)
        raise SSRFError(
            f"DNS resolution failed for {hostname}: {redact_url_credentials(url)}"
        ) from exc

    resolved: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        addr = str(sockaddr[0])
        if _is_blocked_ip(addr):
            raise SSRFError(f"URL {redact_url_credentials(url)} resolves to blocked address {addr}")
        resolved.append(addr)
    return resolved


def validate_url_safe(url: str) -> None:
    """Validate that a URL does not target blocked/internal network ranges.

    Raises:
        ValueError: If hostname cannot be extracted.
        SSRFError: If the URL resolves to a blocked address or hostname, or if
            DNS resolution fails (fail-closed).
    """
    _resolve_and_check(url)


def resolve_public_ip(url: str) -> str:
    """Validate ``url`` and return the single resolved public IP to pin to.

    Same validation as :func:`validate_url_safe`, but returns the first resolved
    address so the caller can connect to that fixed IP instead of letting the
    transport re-resolve the hostname. Pinning that IP is what closes the
    DNS-rebinding TOCTOU window: validate-time and connect-time would otherwise
    resolve the name independently and could differ (public → internal flip).
    Mirrors ``w2v_common.net.validate_public_url``'s return contract.

    Raises:
        ValueError / SSRFError: as :func:`validate_url_safe`.
    """
    resolved = _resolve_and_check(url)
    if not resolved:
        raise SSRFError(f"DNS returned no addresses for {redact_url_credentials(url)}")
    return resolved[0]


def pin_target(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return ``(request_url, headers, extensions)`` for an IP-pinned request.

    The connection target host is rewritten to the literal ``ip`` while the
    original hostname is preserved for the ``Host`` header and for TLS
    (``sni_hostname`` drives both the ClientHello SNI and httpx's certificate
    hostname verification), so a pinned https fetch still verifies the cert
    against the hostname — not the IP. Mirrors
    ``w2v_common.net.safe_fetch._pin`` so this service pins identically to the
    fleet-wide guard.
    """
    parsed = urlparse(url)
    host_bracket = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host_bracket}:{parsed.port}" if parsed.port is not None else host_bracket
    if parsed.username:
        auth = parsed.username + (f":{parsed.password}" if parsed.password else "")
        netloc = f"{auth}@{netloc}"
    pinned_url = urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
    # Trailing root dot stripped: RFC 6066 forbids it in the TLS SNI extension.
    hostname = (parsed.hostname or "").rstrip(".").lower()
    host_disp = f"[{hostname}]" if ":" in hostname else hostname
    host_header = f"{host_disp}:{parsed.port}" if parsed.port is not None else host_disp
    return pinned_url, {"Host": host_header}, {"sni_hostname": hostname}
