"""Inspection comparison (diff) service."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.6


@dataclass
class ScreenChange:
    """Details about how a screen changed between inspections."""

    screen_id_before: str
    screen_id_after: str
    screen_type_before: str
    screen_type_after: str
    activity_name: str | None = None
    text_similarity: float = 0.0
    interactive_elements_before: int = 0
    interactive_elements_after: int = 0
    change_summary: str = ""


@dataclass
class NavigationChange:
    """A navigation edge that was added or removed."""

    from_screen: str
    to_screen: str
    action: str = "tap"
    element_label: str | None = None
    change_type: str = ""  # "added" or "removed"


@dataclass
class InspectionDiffResult:
    """Result of comparing two inspections."""

    added_screens: list[dict] = field(default_factory=list)
    removed_screens: list[dict] = field(default_factory=list)
    changed_screens: list[ScreenChange] = field(default_factory=list)
    unchanged_screen_ids: list[str] = field(default_factory=list)
    navigation_changes: list[NavigationChange] = field(default_factory=list)
    summary: str = ""


@dataclass
class _MatchState:
    matched_before: set[str] = field(default_factory=set)
    matched_after: set[str] = field(default_factory=set)
    changed: list[ScreenChange] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def record(self, before: dict, after: dict) -> None:
        bid = before.get("screen_id", "")
        aid = after.get("screen_id", "")
        self.matched_before.add(bid)
        self.matched_after.add(aid)
        change = _compare_screens(before, after)
        if change:
            self.changed.append(change)
        else:
            self.unchanged.append(aid)


def _index_by_activity(screens: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in screens:
        activity = s.get("activity_name")
        if activity:
            out[activity] = s
    return out


def _match_by_activity(
    screens_before: list[dict], screens_after: list[dict], state: _MatchState
) -> None:
    before_by_activity = _index_by_activity(screens_before)
    after_by_activity = _index_by_activity(screens_after)
    for activity, before_screen in before_by_activity.items():
        after_screen = after_by_activity.get(activity)
        if after_screen is not None:
            state.record(before_screen, after_screen)


def _match_by_similarity(
    screens_before: list[dict], screens_after: list[dict], state: _MatchState
) -> None:
    unmatched_before = [
        s for s in screens_before if s.get("screen_id", "") not in state.matched_before
    ]
    unmatched_after = [
        s for s in screens_after if s.get("screen_id", "") not in state.matched_after
    ]
    for before_s in unmatched_before:
        best_match: dict | None = None
        best_score = _SIMILARITY_THRESHOLD
        for after_s in unmatched_after:
            if after_s.get("screen_id", "") in state.matched_after:
                continue
            score = _screen_similarity(before_s, after_s)
            if score > best_score:
                best_score = score
                best_match = after_s
        if best_match is not None:
            state.record(before_s, best_match)


def _summary(added: list[dict], removed: list[dict], changed: list, unchanged: list) -> str:
    parts: list[str] = []
    if added:
        parts.append(f"{len(added)} screen(s) added")
    if removed:
        parts.append(f"{len(removed)} screen(s) removed")
    if changed:
        parts.append(f"{len(changed)} screen(s) changed")
    if unchanged:
        parts.append(f"{len(unchanged)} screen(s) unchanged")
    return "; ".join(parts) if parts else "No differences found"


def compare_inspections(
    screens_before: list[dict],
    screens_after: list[dict],
    nav_before: list[dict],
    nav_after: list[dict],
) -> InspectionDiffResult:
    """Compare two inspection results and return differences."""
    state = _MatchState()
    _match_by_activity(screens_before, screens_after, state)
    _match_by_similarity(screens_before, screens_after, state)

    added = [s for s in screens_after if s.get("screen_id", "") not in state.matched_after]
    removed = [s for s in screens_before if s.get("screen_id", "") not in state.matched_before]
    nav_changes = _compare_navigation(nav_before, nav_after)

    return InspectionDiffResult(
        added_screens=added,
        removed_screens=removed,
        changed_screens=state.changed,
        unchanged_screen_ids=state.unchanged,
        navigation_changes=nav_changes,
        summary=_summary(added, removed, state.changed, state.unchanged),
    )


def _screen_similarity(a: dict, b: dict) -> float:
    """Compute similarity between two screens using type and text content."""
    score = 0.0

    # Screen type match
    if a.get("screen_type") == b.get("screen_type"):
        score += 0.3

    # Text similarity
    text_a = a.get("text_content", "") or ""
    text_b = b.get("text_content", "") or ""
    if text_a and text_b:
        text_sim = SequenceMatcher(None, text_a[:500], text_b[:500]).ratio()
        score += text_sim * 0.7
    elif not text_a and not text_b:
        score += 0.3

    return score


def _compare_screens(before: dict, after: dict) -> ScreenChange | None:
    """Compare two matched screens. Returns ScreenChange if different, None if unchanged."""
    text_a = before.get("text_content", "") or ""
    text_b = after.get("text_content", "") or ""
    text_sim = (
        SequenceMatcher(None, text_a[:500], text_b[:500]).ratio() if (text_a or text_b) else 1.0
    )

    type_changed = before.get("screen_type") != after.get("screen_type")
    elems_before = before.get("interactive_elements", 0)
    elems_after = after.get("interactive_elements", 0)
    elem_changed = elems_before != elems_after
    text_changed = text_sim < 0.95

    if not type_changed and not elem_changed and not text_changed:
        return None

    parts = []
    if type_changed:
        parts.append(f"type: {before.get('screen_type')} -> {after.get('screen_type')}")
    if elem_changed:
        parts.append(f"elements: {elems_before} -> {elems_after}")
    if text_changed:
        parts.append(f"text similarity: {text_sim:.0%}")

    return ScreenChange(
        screen_id_before=before.get("screen_id", ""),
        screen_id_after=after.get("screen_id", ""),
        screen_type_before=before.get("screen_type", "unknown"),
        screen_type_after=after.get("screen_type", "unknown"),
        activity_name=before.get("activity_name") or after.get("activity_name"),
        text_similarity=text_sim,
        interactive_elements_before=elems_before,
        interactive_elements_after=elems_after,
        change_summary="; ".join(parts),
    )


def _compare_navigation(
    nav_before: list[dict],
    nav_after: list[dict],
) -> list[NavigationChange]:
    """Find navigation edges that were added or removed."""

    def _edge_key(e: dict) -> tuple:
        return (
            e.get("from_screen", ""),
            e.get("to_screen", ""),
            e.get("action", "tap"),
        )

    before_edges = {_edge_key(e) for e in nav_before}
    after_edges = {_edge_key(e) for e in nav_after}

    changes = []

    # Removed edges
    for e in nav_before:
        key = _edge_key(e)
        if key not in after_edges:
            changes.append(
                NavigationChange(
                    from_screen=e.get("from_screen", ""),
                    to_screen=e.get("to_screen", ""),
                    action=e.get("action", "tap"),
                    element_label=e.get("element_label"),
                    change_type="removed",
                )
            )

    # Added edges
    for e in nav_after:
        key = _edge_key(e)
        if key not in before_edges:
            changes.append(
                NavigationChange(
                    from_screen=e.get("from_screen", ""),
                    to_screen=e.get("to_screen", ""),
                    action=e.get("action", "tap"),
                    element_label=e.get("element_label"),
                    change_type="added",
                )
            )

    return changes
