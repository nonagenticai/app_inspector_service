"""Classify mobile app screens by type using heuristics."""

from __future__ import annotations

import re

# Screen type constants
LOGIN = "login"
ONBOARDING = "onboarding"
SETTINGS = "settings"
SEARCH = "search"
PROFILE = "profile"
MODAL = "modal"
FORM = "form"
LIST = "list"
TAB_BAR = "tab_bar"
DETAIL = "detail"
HOME = "home"
UNKNOWN = "unknown"

ALL_SCREEN_TYPES = [
    LOGIN,
    ONBOARDING,
    SETTINGS,
    SEARCH,
    PROFILE,
    MODAL,
    FORM,
    LIST,
    TAB_BAR,
    DETAIL,
    HOME,
    UNKNOWN,
]

# Patterns for classification
_LOGIN_PATTERNS = re.compile(
    r"(password|sign.?in|log.?in|username|email.*password|auth)", re.IGNORECASE
)
_ONBOARDING_PATTERNS = re.compile(
    r"(welcome|get.?started|tutorial|intro|onboard|skip|next.*page)", re.IGNORECASE
)
_SETTINGS_PATTERNS = re.compile(
    r"(settings|preferences|toggle|switch.*on|switch.*off|notification)", re.IGNORECASE
)
_SEARCH_PATTERNS = re.compile(r"(search|find|filter|query|magnifying)", re.IGNORECASE)
_PROFILE_PATTERNS = re.compile(
    r"(profile|my.?account|avatar|edit.?profile|user.?info)", re.IGNORECASE
)
_FORM_PATTERNS = re.compile(
    r"(form|input|text.?field|submit|register|sign.?up|create.?account)", re.IGNORECASE
)
_LIST_PATTERNS = re.compile(r"(list|recycler|table|collection|scroll|feed|timeline)", re.IGNORECASE)


# Pattern-based classification table (order matters - more specific first)
_PATTERN_CLASSIFICATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_LOGIN_PATTERNS, LOGIN),
    (_ONBOARDING_PATTERNS, ONBOARDING),
    (_SETTINGS_PATTERNS, SETTINGS),
    (_SEARCH_PATTERNS, SEARCH),
    (_PROFILE_PATTERNS, PROFILE),
    (_FORM_PATTERNS, FORM),
    (_LIST_PATTERNS, LIST),
)


def _has_password_field(xml_source: str) -> bool:
    """Return True if XML contains a secure-text/password edit field."""
    return "password" in xml_source.lower() and (
        "XCUIElementTypeSecureTextField" in xml_source or "android.widget.EditText" in xml_source
    )


def _classify_by_pattern(combined: str) -> str | None:
    """Run regex-based classification rules in priority order."""
    for pattern, label in _PATTERN_CLASSIFICATIONS:
        if pattern.search(combined):
            return label
    return None


def _classify_by_activity(activity_name: str | None) -> str | None:
    """Classify by activity name keywords; returns None if no match."""
    if not activity_name:
        return None
    activity_lower = activity_name.lower()
    if any(kw in activity_lower for kw in ("main", "home", "launcher")):
        return HOME
    if any(kw in activity_lower for kw in ("detail", "view")):
        return DETAIL
    return None


def classify_screen(
    xml_source: str,
    activity_name: str | None = None,
    text_content: str | None = None,
    accessibility_ids: list[str] | None = None,
) -> str:
    """Classify a screen based on its XML source, activity, and text content.

    Returns one of the screen type constants.
    """
    combined = (xml_source or "") + " " + (activity_name or "") + " " + (text_content or "")
    if accessibility_ids:
        combined += " " + " ".join(accessibility_ids)

    if _has_password_field(xml_source):
        return LOGIN

    pattern_match = _classify_by_pattern(combined)
    if pattern_match is not None:
        return pattern_match

    activity_match = _classify_by_activity(activity_name)
    if activity_match is not None:
        return activity_match

    if "BottomNavigation" in xml_source or "UITabBar" in xml_source:
        return TAB_BAR

    return UNKNOWN
