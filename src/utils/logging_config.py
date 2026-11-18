"""Logging clamps for third-party loggers that would leak our credentials.

``src/asgi.py`` configures the root logger from ``LOG_LEVEL``. That knob is
operator-facing, and the operator most likely to turn it to ``DEBUG`` is the one
debugging a device-farm session — precisely the run whose Appium hub URL carries
``<user>:<access_key>@`` inline. Redacting at our own log site is not enough if a
dependency re-emits the same URL one level down, so the leaking logger gets a
floor it cannot be argued below.

The floor is repo-local on purpose: it is applied to named loggers from this
service's own logging setup, not installed into ``w2v_common``, so no other
fleet service changes behaviour.
"""

from __future__ import annotations

import logging

#: Loggers that emit a credentialed URL verbatim at DEBUG.
#:
#: Membership is by demonstration, not by suspicion. Each name below was
#: confirmed by driving ``appium.webdriver.Remote`` at a
#: ``https://<user>:<key>@…`` hub with the root logger at DEBUG and capturing
#: every ``LogRecord``:
#:
#: * ``selenium.webdriver.remote.remote_connection`` — LEAKS. It logs
#:   ``"%s %s %s" % (method, url, body)`` where ``url`` is
#:   ``client_config.remote_server_addr + path``, i.e. userinfo included.
#: * ``urllib3.connectionpool`` / ``urllib3.util.retry`` — emit, do NOT leak.
#:   urllib3 only ever formats ``self.host``/``self.port`` and the *path*; the
#:   userinfo never reaches its log calls.
#: * ``httpx`` — never receives a credentialed URL from this service.
#:   ``src/services/device_farm.py`` authenticates with ``auth=(user, key)``
#:   tuples, so the userinfo is not in the URL httpx logs.
#: * ``appium`` — warns about embedded credentials via ``warnings.warn``, and
#:   the warning text does not contain the URL.
CREDENTIAL_LEAKING_DEBUG_LOGGERS: tuple[str, ...] = ("selenium.webdriver.remote.remote_connection",)

#: No logger in the tuple above may ever be more verbose than this.
MINIMUM_LEVEL = logging.INFO


def clamp_credential_leaking_loggers() -> None:
    """Pin every credential-leaking third-party logger at or above INFO.

    The level is ``max(root_effective_level, INFO)``: a stricter root (the
    normal ``WARNING``/``INFO`` production setting) is still honoured, so this
    adds no log volume — it only removes DEBUG as a reachable level for these
    loggers, whatever ``LOG_LEVEL`` says.
    """
    level = max(logging.getLogger().getEffectiveLevel(), MINIMUM_LEVEL)
    for name in CREDENTIAL_LEAKING_DEBUG_LOGGERS:
        logging.getLogger(name).setLevel(level)
