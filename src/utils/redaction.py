"""Redaction helpers for values that are safe to use but unsafe to log.

Device-farm Appium hub URLs carry their credentials inline
(``https://<user>:<access_key>@hub-cloud.browserstack.com/wd/hub``) because
that is the only way the Appium client can authenticate. The URL therefore
MUST keep its userinfo in memory — redaction belongs at the log site, never at
construction.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from src.api.constants import MAX_ERROR_LENGTH

_MASK = "***"

# Substrings that mark a query PARAMETER NAME as carrying a secret value.
# Matched case-insensitively against the parameter name. Covers AWS SigV4 and
# SigV2 presigned URLs (``X-Amz-Signature``, ``X-Amz-Credential``,
# ``X-Amz-Security-Token``, ``Signature``, ``AWSAccessKeyId``) as well as the
# generic bearer/API-key shapes MinIO and CDN links use.
_SECRET_PARAM_HINTS = (
    "sig",
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "key",
    "credential",
    "auth",
)

# A URL embedded in free-form prose. Stops at whitespace and at the characters
# that conventionally terminate a URL in an English sentence, so a trailing
# ``: connection refused`` is not swallowed into the match.
#
#: The scheme is ``\b``-anchored and BOUNDED: an unbounded, unanchored
#: ``[a-zA-Z][a-zA-Z0-9+.\-]*://`` retries from every offset of a long alnum run,
#: which is quadratic (measured 200k chars -> 25.6 s inside the event loop).
_URL_IN_TEXT_RE = re.compile(r"""\b[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://[^\s<>"'`]+""")


def _is_secret_param(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_PARAM_HINTS)


def _redact_query(query: str) -> str:
    """Replace the values of secret-named parameters, preserving order/names."""
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return query
    redacted = [(name, _MASK if _is_secret_param(name) else value) for name, value in pairs]
    if redacted == pairs:
        return query
    return urlencode(redacted)


def redact_url_credentials(url: str) -> str:
    """Return ``url`` with any inline userinfo (``user:password@``) masked.

    A URL without userinfo is returned unchanged. This function never raises:
    anything it cannot parse collapses to a bare mask, because the only callers
    are logging/telemetry sites where an exception would be worse than a
    useless-but-safe string.

    A hub URL whose netloc carries ``<user>:<access_key>@`` comes back as
    ``https://***:***@hub-cloud.browserstack.com/wd/hub`` — scheme, host, port,
    path, query and fragment are all preserved so the line stays diagnosable.
    ``http://localhost:4723`` comes back byte-identical.

    An input with **no authority** (no ``//``, so ``urlsplit`` leaves the whole
    string in the path) but which still contains ``@`` collapses to a bare mask.
    ``get_appium_url`` always emits ``https://``, but ``appium_url`` is a
    user-supplied override, and a hand-written ``user:pw@host/wd/hub`` would
    otherwise be returned verbatim: there is no netloc to mask and no reliable
    way to re-split it, so the safe answer is to emit nothing.

    Query-string secrets ARE masked. ``app_path`` is caller-supplied and a
    presigned URL carries its secret in the QUERY STRING, not in userinfo
    (``?X-Amz-Signature=…``, ``?token=…``), so userinfo-only redaction left the
    more common shape leaking. Only the VALUES of parameters whose NAME matches
    :data:`_SECRET_PARAM_HINTS` are replaced; every other parameter — and every
    parameter NAME — survives, so the line stays diagnosable. The hint list
    deliberately over-matches (``X-Amz-SignedHeaders`` is masked too): a
    needlessly masked value costs a debugging detail, an unmasked one costs a
    credential.
    """
    try:
        parts = urlsplit(url)
        query = _redact_query(parts.query)
        if "@" in parts.netloc:
            userinfo, _, host = parts.netloc.rpartition("@")
            masked_userinfo = f"{_MASK}:{_MASK}" if ":" in userinfo else _MASK
            return urlunsplit(
                (parts.scheme, f"{masked_userinfo}@{host}", parts.path, query, parts.fragment)
            )
        if not parts.netloc and "@" in url:
            return _MASK
        if query != parts.query:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
        return url
    except AttributeError, TypeError, ValueError, UnicodeError:
        return _MASK


def redact_text(text: str) -> str:
    """Mask credentials in every URL-looking token inside a free-form string.

    Exception messages are composed, not templated: ``f"Failed to download
    {url}: {exc}"`` embeds the URL twice — once from our own placeholder and
    once from httpx's ``str(exc)``, which quotes the (still credentialed)
    request URL. Redacting only the placeholder therefore leaves the secret in
    the tail. This scrubs the WHOLE message, so any URL that arrives from a
    third-party library or a nested cause is covered too.
    """
    if not isinstance(text, str) or "://" not in text:
        return text
    return _URL_IN_TEXT_RE.sub(lambda m: redact_url_credentials(m.group(0)), text)


# ---------------------------------------------------------------------------
# Job error messages
# ---------------------------------------------------------------------------
#
# ``jobs.error_message`` is returned verbatim by ``GET /api/inspect/{id}`` to the
# orchestrator (whose w2v_common ``poll_job`` logs the full response) and can be
# shown in a browser. A device-farm failure can quote the tenant's access key in
# PROSE -- not inside a URL -- so :func:`redact_text` alone is not enough.
# w2v_common's key-name masker (``clients.base._redact_for_log``) is private, so
# the equivalent rule lives here.

#: A key whose NAME ends in a credential name, followed by ``=`` or ``:`` and a
#: value: ``access_key=…``, ``"browserstack_access_key": "…"``, ``password: …``.
#: ``[\w-]{0,64}?`` is a bounded, unanchored prefix so ``browserstack_access_key``
#: matches (``\b`` never fires inside ``_``-joined names). ``max_tokens`` /
#: ``password_policy`` are untouched: the name must be followed by the separator.
_CREDENTIAL_KV_RE = re.compile(
    r"""(?ix)
    (?P<name>[\w-]{0,64}?
        (?:api[_-]?key|apikey|client[_-]?secret|password|passwd|secret|
           access[_-]?key(?:[_-]?id)?|private[_-]?key|refresh[_-]?token|
           session[_-]?key|access[_-]?token|auth[_-]?token|id[_-]?token|
           bearer[_-]?token|token)
    )
    (?P<sep>(?:\\?["'])?\s*[:=]\s*(?:\\?["'])?)
    (?P<value>[^\s"',;&}\]\\]+)
    """
)

#: ``Authorization: Basic <b64>`` / ``Bearer <token>`` quoted back by an HTTP
#: library. Minimum length keeps prose like "basic auth" untouched.
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?P<scheme>basic|bearer)\s+[A-Za-z0-9+/=._~-]{8,}")

#: ``scheme://<anything>@`` -- userinfo that ``urlsplit`` cannot see because the
#: secret itself contains ``/`` (``https://u:ab/cd@hub/...`` splits the netloc at
#: the first ``/``, so :func:`redact_url_credentials` leaves it alone). Error text
#: only: an ``@`` in a URL path gets over-masked, which costs a detail, not a key.
_USERINFO_IN_TEXT_RE = re.compile(
    r"""(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://)(?P<info>[^\s<>"'`@]{1,512})@"""
)


def _mask_userinfo(m: re.Match[str]) -> str:
    masked = f"{_MASK}:{_MASK}" if ":" in m.group("info") else _MASK
    return f"{m.group('scheme')}{masked}@"


#: Regex layers run on at most this many characters. The exact-value scrub runs
#: on the FULL text first (it is linear ``str.replace``); everything after it is
#: truncated, so no adversarial ``str(exc)`` can make the pattern layers slow.
#: 2x the persisted cap: JobManager truncates to MAX_ERROR_LENGTH afterwards.
_REGEX_INPUT_CAP = 2 * MAX_ERROR_LENGTH

#: A username shorter than this is not scrubbed as a bare token: a 2-letter
#: username would shred ordinary words out of the message. The ACCESS KEY is
#: always scrubbed, whatever its length -- over-masking beats leaking.
_MIN_BARE_USERNAME_LEN = 4


def _b64_variants(raw: str) -> set[str]:
    encoded = raw.encode()
    out: set[str] = set()
    for b in (base64.b64encode(encoded), base64.urlsafe_b64encode(encoded)):
        text = b.decode("ascii")
        out.add(text)
        out.add(text.rstrip("="))
    return {v for v in out if v}


def _encoded_variants(raw: str) -> set[str]:
    """``raw`` plus every encoding a transport/library is known to quote back."""
    return {raw, quote(raw, safe=""), quote(raw), quote_plus(raw), quote(raw, safe="@:")}


def secret_variants(username: str | None, access_key: str | None) -> list[str]:
    """Every textual form the credential pair can appear in, longest first.

    Covers the raw values, ``user:key``, ``user:key@`` hub userinfo, HTTP Basic
    ``base64(user:key)`` (standard + urlsafe, padded + unpadded), and the
    percent-encoded forms of each. Longest-first so a composite form is masked
    whole before its pieces are.
    """
    variants: set[str] = set()
    if access_key:
        variants |= _encoded_variants(access_key)
        variants |= _b64_variants(access_key)
        if username:
            pair = f"{username}:{access_key}"
            variants |= _encoded_variants(pair)
            variants |= _b64_variants(pair)
            # userinfo where only one half was percent-encoded
            variants.add(f"{quote(username, safe='')}:{quote(access_key, safe='')}")
    if username and len(username) >= _MIN_BARE_USERNAME_LEN:
        variants |= _encoded_variants(username)
    return sorted((v for v in variants if v), key=len, reverse=True)


def scrub_known_secrets(text: str, secrets: list[str]) -> str:
    """Replace every exact occurrence of each value in ``secrets`` with a mask."""
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, _MASK)
    return text


def redact_error_message(
    text: str,
    *,
    username: str | None = None,
    access_key: str | None = None,
) -> str:
    """Scrub a job error message before it is persisted or returned.

    Three layers, all applied:

    1. exact-value scrubbing of the KNOWN credentials of the current request
       (:func:`secret_variants`) -- the only layer that catches a bare key in
       prose, which no pattern can recognise;
    2. :func:`redact_text` -- userinfo / secret query params inside URLs, plus
       any ``scheme://…@`` userinfo a URL parser would mis-split;
    3. key-name masking (``access_key=…``) and ``Basic``/``Bearer`` values, for
       credentials this process does not hold (a server-side key, a key from a
       stale row).

    Output is capped at ``2 * MAX_ERROR_LENGTH`` characters (see
    :data:`_REGEX_INPUT_CAP`). Never raises: a scrubber that throws on the failure path would lose the
    failure itself.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        text = scrub_known_secrets(text, secret_variants(username, access_key))
        text = text[:_REGEX_INPUT_CAP]
        if "://" in text:
            text = redact_text(text)
            text = _USERINFO_IN_TEXT_RE.sub(_mask_userinfo, text)
        text = _CREDENTIAL_KV_RE.sub(lambda m: f"{m.group('name')}{m.group('sep')}{_MASK}", text)
        return _AUTH_SCHEME_RE.sub(lambda m: f"{m.group('scheme')} {_MASK}", text)
    except Exception:  # noqa: BLE001 -- see docstring
        return _MASK
