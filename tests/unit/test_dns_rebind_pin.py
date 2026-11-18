"""DNS-rebind pinning proof for the app-download path.

Regression guard for the TOCTOU window in ``AppAcquisitionService._download_file``:
the SSRF validator resolved the hostname and approved a *public* IP, but the
``httpx`` client then re-resolved the same hostname independently at connect
time. A hostname that flips public→internal between those two resolutions
(``rebind.evil`` → ``93.184.216.34`` for the guard, ``169.254.169.254`` for the
connect) reached the internal address anyway.

The fix pins the connection to the exact IP the guard resolved. This test proves
the outgoing request targets that PINNED public IP (host rewritten to the IP
literal, ``Host`` header + TLS ``sni_hostname`` kept as the original hostname),
rather than the bare hostname the transport would have re-resolved.

No network: ``getaddrinfo`` is stubbed to a public IP and the httpx transport is
a ``MockTransport`` that records the request it is asked to send.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx

from src.services.app_acquisition import AppAcquisitionService

_HOSTNAME = "rebind.evil"
_PUBLIC_IP = "93.184.216.34"  # what the SSRF guard resolves + must pin to
_URL = f"https://{_HOSTNAME}/app.apk"


def _public_getaddrinfo(host, *_args, **_kwargs):
    """Resolve the guarded hostname to a single PUBLIC IP (the validated addr)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]


class TestDnsRebindPin(unittest.TestCase):
    """The download connection must target the SSRF-validated IP, not re-resolve."""

    @patch("src.helpers.url_validation.socket.getaddrinfo", _public_getaddrinfo)
    @patch("src.services.app_acquisition.get_settings")
    def test_download_connection_is_pinned_to_validated_ip(self, mock_settings):
        captured: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, headers={"content-length": "2"}, content=b"PK")

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                acquisition=MagicMock(
                    download_timeout=30,
                    max_file_size_mb=500,
                    temp_dir=tmpdir,
                    allowed_local_paths=[tmpdir],
                )
            )
            svc = AppAcquisitionService()

            with patch("src.services.app_acquisition.httpx.AsyncClient", client_factory):
                asyncio.run(svc._download_file(_URL, suffix=".apk"))

        req = captured["request"]
        # The connection target host is the validated PUBLIC IP literal — the
        # transport is never handed the bare hostname to re-resolve. This is the
        # assertion that FAILS on the pre-fix code (which streamed the raw URL).
        self.assertEqual(req.url.host, _PUBLIC_IP)
        # Host header + TLS SNI stay the original hostname so vhost routing and
        # https certificate verification still work against the name.
        self.assertEqual(req.headers["host"], _HOSTNAME)
        self.assertEqual(req.extensions.get("sni_hostname"), _HOSTNAME)


if __name__ == "__main__":
    unittest.main()
