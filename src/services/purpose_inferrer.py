"""Infer app purpose from inspection data."""

from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger(__name__)

# Screen type to domain mapping
_DOMAIN_SIGNALS: dict[str, list[str]] = {
    "social_media": [
        "profile",
        "feed",
        "timeline",
        "post",
        "comment",
        "like",
        "follow",
        "share",
    ],
    "e_commerce": [
        "cart",
        "checkout",
        "product",
        "price",
        "buy",
        "shop",
        "order",
        "payment",
    ],
    "productivity": [
        "task",
        "calendar",
        "note",
        "reminder",
        "project",
        "document",
        "todo",
    ],
    "communication": [
        "chat",
        "message",
        "call",
        "contact",
        "inbox",
        "conversation",
    ],
    "media": [
        "player",
        "video",
        "audio",
        "podcast",
        "music",
        "stream",
        "playlist",
    ],
    "finance": [
        "bank",
        "transfer",
        "balance",
        "transaction",
        "account",
        "pay",
        "wallet",
    ],
    "health": [
        "fitness",
        "health",
        "workout",
        "step",
        "heart",
        "sleep",
        "nutrition",
    ],
    "education": [
        "course",
        "lesson",
        "quiz",
        "learn",
        "study",
        "class",
        "grade",
    ],
    "travel": [
        "map",
        "booking",
        "flight",
        "hotel",
        "trip",
        "destination",
        "route",
    ],
    "food": [
        "menu",
        "restaurant",
        "order",
        "delivery",
        "food",
        "recipe",
        "cuisine",
    ],
}


def _score_domains(all_text: str) -> dict[str, int]:
    """Score every known domain by keyword occurrences in the combined text."""
    scores: dict[str, int] = {}
    for domain, keywords in _DOMAIN_SIGNALS.items():
        score = sum(all_text.count(kw) for kw in keywords)
        if score > 0:
            scores[domain] = score
    return scores


def _describe_primary_domain(
    domain_scores: dict[str, int],
    *,
    has_login: bool,
    has_forms: bool,
    has_list: bool,
    has_search: bool,
    has_tab_bar: bool,
) -> str:
    """Describe the primary domain or fall back to UI-pattern inference."""
    if domain_scores:
        top_domain = max(domain_scores, key=lambda k: domain_scores[k])
        return f"This appears to be a {top_domain.replace('_', ' ')} application"
    if has_list and has_search:
        return "This appears to be a content browsing application"
    if has_forms and has_login:
        return "This appears to be a form-based application with user accounts"
    if has_tab_bar:
        return "This is a multi-section application with tab-based navigation"
    return "This is a general-purpose mobile application"


def _describe_navigation(screen_count: int, edge_count: int) -> str:
    """Describe navigation complexity from screen/edge counts."""
    if edge_count > screen_count * 2:
        return "with complex, interconnected navigation"
    if edge_count > screen_count:
        return "with moderate navigation depth"
    return "with simple, linear navigation"


def _collect_features(
    *,
    has_login: bool,
    has_search: bool,
    has_list: bool,
    has_forms: bool,
) -> list[str]:
    """Collect human-readable feature labels from boolean flags."""
    features: list[str] = []
    if has_login:
        features.append("user authentication")
    if has_search:
        features.append("search functionality")
    if has_list:
        features.append("list-based content")
    if has_forms:
        features.append("data entry forms")
    return features


def infer_purpose_heuristic(
    screens: list[dict],
    edges: list[dict],
) -> str:
    """Infer app purpose from collected screen data and navigation patterns.

    Returns a 1-2 sentence description of the app's likely purpose.
    """
    if not screens:
        return "Unable to determine app purpose: no screens discovered."

    all_text = " ".join((s.get("text_content", "") or "").lower() for s in screens)
    type_counts = Counter(s.get("screen_type", "unknown") for s in screens)

    domain_scores = _score_domains(all_text)
    has_login = "login" in type_counts
    has_forms = any(s.get("has_forms", False) for s in screens)
    has_list = "list" in type_counts
    has_search = "search" in type_counts
    has_tab_bar = "tab_bar" in type_counts

    parts: list[str] = [
        _describe_primary_domain(
            domain_scores,
            has_login=has_login,
            has_forms=has_forms,
            has_list=has_list,
            has_search=has_search,
            has_tab_bar=has_tab_bar,
        ),
        _describe_navigation(len(screens), len(edges)),
    ]

    features = _collect_features(
        has_login=has_login,
        has_search=has_search,
        has_list=has_list,
        has_forms=has_forms,
    )
    if features:
        parts.append(f"featuring {', '.join(features[:3])}")

    return ". ".join(parts) + "."
