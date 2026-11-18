"""Tests for the inspection comparison (diff) service."""

from __future__ import annotations

from src.services.inspection_diff import (
    InspectionDiffResult,
    ScreenChange,
    _compare_navigation,
    _compare_screens,
    _screen_similarity,
    compare_inspections,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _screen(
    screen_id: str,
    screen_type: str = "content",
    activity_name: str | None = None,
    text_content: str | None = None,
    interactive_elements: int = 3,
) -> dict:
    """Build a minimal screen dict for testing."""
    return {
        "screen_id": screen_id,
        "screen_type": screen_type,
        "activity_name": activity_name,
        "text_content": text_content,
        "interactive_elements": interactive_elements,
    }


def _nav(
    from_screen: str,
    to_screen: str,
    action: str = "tap",
    element_label: str | None = None,
) -> dict:
    return {
        "from_screen": from_screen,
        "to_screen": to_screen,
        "action": action,
        "element_label": element_label,
    }


# ---------------------------------------------------------------------------
# Tests: empty inputs
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    def test_both_empty(self) -> None:
        result = compare_inspections([], [], [], [])
        assert isinstance(result, InspectionDiffResult)
        assert result.added_screens == []
        assert result.removed_screens == []
        assert result.changed_screens == []
        assert result.unchanged_screen_ids == []
        assert result.navigation_changes == []
        assert result.summary == "No differences found"

    def test_empty_before_only(self) -> None:
        after = [_screen("s1")]
        result = compare_inspections([], after, [], [])
        assert len(result.added_screens) == 1
        assert result.removed_screens == []
        assert "1 screen(s) added" in result.summary

    def test_empty_after_only(self) -> None:
        before = [_screen("s1")]
        result = compare_inspections(before, [], [], [])
        assert result.added_screens == []
        assert len(result.removed_screens) == 1
        assert "1 screen(s) removed" in result.summary


# ---------------------------------------------------------------------------
# Tests: identical screens (no changes)
# ---------------------------------------------------------------------------


class TestIdenticalScreens:
    def test_single_identical_screen_matched_by_activity(self) -> None:
        s = _screen("s1", activity_name="com.app.Main", text_content="Hello")
        result = compare_inspections([s], [s], [], [])
        assert result.added_screens == []
        assert result.removed_screens == []
        assert result.changed_screens == []
        assert result.unchanged_screen_ids == ["s1"]
        assert "1 screen(s) unchanged" in result.summary

    def test_multiple_identical_screens(self) -> None:
        screens = [
            _screen("s1", activity_name="com.app.Main", text_content="Home"),
            _screen("s2", activity_name="com.app.Settings", text_content="Settings"),
        ]
        result = compare_inspections(screens, screens, [], [])
        assert len(result.unchanged_screen_ids) == 2
        assert result.changed_screens == []
        assert result.added_screens == []
        assert result.removed_screens == []


# ---------------------------------------------------------------------------
# Tests: added screens only
# ---------------------------------------------------------------------------


class TestAddedScreens:
    def test_one_screen_added(self) -> None:
        before = [_screen("s1", activity_name="com.app.Main")]
        after = [
            _screen("s1", activity_name="com.app.Main"),
            _screen("s2", activity_name="com.app.New"),
        ]
        result = compare_inspections(before, after, [], [])
        assert len(result.added_screens) == 1
        assert result.added_screens[0]["screen_id"] == "s2"
        assert result.removed_screens == []

    def test_multiple_screens_added(self) -> None:
        before = [_screen("s1", activity_name="com.app.Main")]
        after = [
            _screen("s1", activity_name="com.app.Main"),
            _screen("s2", activity_name="com.app.A"),
            _screen("s3", activity_name="com.app.B"),
        ]
        result = compare_inspections(before, after, [], [])
        assert len(result.added_screens) == 2
        assert "2 screen(s) added" in result.summary


# ---------------------------------------------------------------------------
# Tests: removed screens only
# ---------------------------------------------------------------------------


class TestRemovedScreens:
    def test_one_screen_removed(self) -> None:
        before = [
            _screen("s1", activity_name="com.app.Main"),
            _screen("s2", activity_name="com.app.Old"),
        ]
        after = [_screen("s1", activity_name="com.app.Main")]
        result = compare_inspections(before, after, [], [])
        assert len(result.removed_screens) == 1
        assert result.removed_screens[0]["screen_id"] == "s2"
        assert result.added_screens == []

    def test_multiple_screens_removed(self) -> None:
        before = [
            _screen("s1", activity_name="com.app.Main"),
            _screen("s2", activity_name="com.app.A"),
            _screen("s3", activity_name="com.app.B"),
        ]
        after = [_screen("s1", activity_name="com.app.Main")]
        result = compare_inspections(before, after, [], [])
        assert len(result.removed_screens) == 2
        assert "2 screen(s) removed" in result.summary


# ---------------------------------------------------------------------------
# Tests: changed screens
# ---------------------------------------------------------------------------


class TestChangedScreens:
    def test_screen_type_change(self) -> None:
        before = [_screen("s1", screen_type="login", activity_name="com.app.Login")]
        after = [_screen("s1", screen_type="form", activity_name="com.app.Login")]
        result = compare_inspections(before, after, [], [])
        assert len(result.changed_screens) == 1
        c = result.changed_screens[0]
        assert c.screen_type_before == "login"
        assert c.screen_type_after == "form"
        assert "type:" in c.change_summary

    def test_element_count_change(self) -> None:
        before = [_screen("s1", activity_name="com.app.Main", interactive_elements=5)]
        after = [_screen("s1", activity_name="com.app.Main", interactive_elements=10)]
        result = compare_inspections(before, after, [], [])
        assert len(result.changed_screens) == 1
        c = result.changed_screens[0]
        assert c.interactive_elements_before == 5
        assert c.interactive_elements_after == 10
        assert "elements:" in c.change_summary

    def test_text_content_change(self) -> None:
        before = [
            _screen(
                "s1",
                activity_name="com.app.Main",
                text_content="Welcome to the app",
            )
        ]
        after = [
            _screen(
                "s1",
                activity_name="com.app.Main",
                text_content="Something completely different here",
            )
        ]
        result = compare_inspections(before, after, [], [])
        assert len(result.changed_screens) == 1
        c = result.changed_screens[0]
        assert c.text_similarity < 0.95
        assert "text similarity:" in c.change_summary

    def test_no_change_when_text_very_similar(self) -> None:
        """Screens with text_similarity >= 0.95 are considered unchanged."""
        text = "This is a long paragraph of text that stays the same."
        before = [_screen("s1", activity_name="com.app.Main", text_content=text)]
        after = [_screen("s1", activity_name="com.app.Main", text_content=text)]
        result = compare_inspections(before, after, [], [])
        assert result.changed_screens == []
        assert "s1" in result.unchanged_screen_ids

    def test_mixed_changes(self) -> None:
        """Multiple change dimensions at once."""
        before = [
            _screen(
                "s1",
                screen_type="list",
                activity_name="com.app.Main",
                text_content="Old list items",
                interactive_elements=4,
            )
        ]
        after = [
            _screen(
                "s1",
                screen_type="grid",
                activity_name="com.app.Main",
                text_content="New grid items completely different layout",
                interactive_elements=8,
            )
        ]
        result = compare_inspections(before, after, [], [])
        assert len(result.changed_screens) == 1
        c = result.changed_screens[0]
        assert "type:" in c.change_summary
        assert "elements:" in c.change_summary


# ---------------------------------------------------------------------------
# Tests: activity_name matching takes priority
# ---------------------------------------------------------------------------


class TestActivityMatchPriority:
    def test_activity_match_over_text_similarity(self) -> None:
        """Even if text is very different, activity_name match should pair them."""
        before = [
            _screen(
                "s1",
                activity_name="com.app.Main",
                text_content="Alpha bravo charlie",
            )
        ]
        after = [
            _screen(
                "s2",
                activity_name="com.app.Main",
                text_content="Completely unrelated delta echo foxtrot",
            )
        ]
        result = compare_inspections(before, after, [], [])
        # They should be matched (changed or unchanged), NOT added/removed
        assert result.added_screens == []
        assert result.removed_screens == []

    def test_activity_match_consumes_screen(self) -> None:
        """Once matched by activity, a screen is not available for text matching."""
        before = [
            _screen("s1", activity_name="com.app.A", text_content="Shared text"),
            _screen("s2", text_content="Shared text"),
        ]
        after = [
            _screen("s3", activity_name="com.app.A", text_content="Changed"),
            _screen("s4", text_content="Shared text"),
        ]
        result = compare_inspections(before, after, [], [])
        # s1 matched to s3 by activity; s2 matched to s4 by text similarity
        assert result.added_screens == []
        assert result.removed_screens == []


# ---------------------------------------------------------------------------
# Tests: text similarity matching for screens without activity names
# ---------------------------------------------------------------------------


class TestTextSimilarityMatching:
    def test_similar_text_matches(self) -> None:
        """Screens without activity names matched by text + type similarity."""
        before = [_screen("s1", screen_type="content", text_content="Welcome to the app")]
        after = [_screen("s2", screen_type="content", text_content="Welcome to the app!")]
        result = compare_inspections(before, after, [], [])
        # Should be matched (not added/removed)
        assert result.added_screens == []
        assert result.removed_screens == []

    def test_dissimilar_text_no_match(self) -> None:
        """Screens with completely different text should not match."""
        before = [_screen("s1", screen_type="content", text_content="AAAAAAA")]
        after = [_screen("s2", screen_type="form", text_content="ZZZZZZZ")]
        result = compare_inspections(before, after, [], [])
        assert len(result.added_screens) == 1
        assert len(result.removed_screens) == 1

    def test_same_type_boosts_similarity(self) -> None:
        """Same screen_type should boost similarity score."""
        s1 = _screen("s1", screen_type="login", text_content="Username Password")
        s2_same_type = _screen("s2", screen_type="login", text_content="User Pass")
        s2_diff_type = _screen("s3", screen_type="dashboard", text_content="User Pass")
        assert _screen_similarity(s1, s2_same_type) > _screen_similarity(s1, s2_diff_type)


# ---------------------------------------------------------------------------
# Tests: navigation change detection
# ---------------------------------------------------------------------------


class TestNavigationChanges:
    def test_no_nav_changes(self) -> None:
        nav = [_nav("s1", "s2"), _nav("s2", "s3")]
        changes = _compare_navigation(nav, nav)
        assert changes == []

    def test_added_edge(self) -> None:
        nav_before = [_nav("s1", "s2")]
        nav_after = [_nav("s1", "s2"), _nav("s2", "s3")]
        changes = _compare_navigation(nav_before, nav_after)
        assert len(changes) == 1
        assert changes[0].change_type == "added"
        assert changes[0].from_screen == "s2"
        assert changes[0].to_screen == "s3"

    def test_removed_edge(self) -> None:
        nav_before = [_nav("s1", "s2"), _nav("s2", "s3")]
        nav_after = [_nav("s1", "s2")]
        changes = _compare_navigation(nav_before, nav_after)
        assert len(changes) == 1
        assert changes[0].change_type == "removed"
        assert changes[0].from_screen == "s2"
        assert changes[0].to_screen == "s3"

    def test_added_and_removed_edges(self) -> None:
        nav_before = [_nav("s1", "s2")]
        nav_after = [_nav("s1", "s3")]
        changes = _compare_navigation(nav_before, nav_after)
        removed = [c for c in changes if c.change_type == "removed"]
        added = [c for c in changes if c.change_type == "added"]
        assert len(removed) == 1
        assert len(added) == 1

    def test_empty_nav(self) -> None:
        changes = _compare_navigation([], [])
        assert changes == []

    def test_edge_action_matters(self) -> None:
        """Edges with different actions are considered different."""
        nav_before = [_nav("s1", "s2", action="tap")]
        nav_after = [_nav("s1", "s2", action="long_press")]
        changes = _compare_navigation(nav_before, nav_after)
        assert len(changes) == 2  # one removed, one added

    def test_nav_in_full_compare(self) -> None:
        """Navigation changes appear in full compare_inspections result."""
        nav_before = [_nav("s1", "s2")]
        nav_after = [_nav("s1", "s2"), _nav("s2", "s3")]
        result = compare_inspections([], [], nav_before, nav_after)
        assert len(result.navigation_changes) == 1
        assert result.navigation_changes[0].change_type == "added"


# ---------------------------------------------------------------------------
# Tests: summary string generation
# ---------------------------------------------------------------------------


class TestSummaryGeneration:
    def test_no_differences_summary(self) -> None:
        result = compare_inspections([], [], [], [])
        assert result.summary == "No differences found"

    def test_only_added_summary(self) -> None:
        result = compare_inspections([], [_screen("s1")], [], [])
        assert result.summary == "1 screen(s) added"

    def test_only_removed_summary(self) -> None:
        result = compare_inspections([_screen("s1")], [], [], [])
        assert result.summary == "1 screen(s) removed"

    def test_only_unchanged_summary(self) -> None:
        s = _screen("s1", activity_name="com.app.Main", text_content="Hello")
        result = compare_inspections([s], [s], [], [])
        assert result.summary == "1 screen(s) unchanged"

    def test_mixed_summary(self) -> None:
        before = [
            _screen("s1", activity_name="com.app.Main", interactive_elements=5),
            _screen("s2", activity_name="com.app.Old"),
        ]
        after = [
            _screen("s1", activity_name="com.app.Main", interactive_elements=10),
            _screen("s3", activity_name="com.app.New"),
        ]
        result = compare_inspections(before, after, [], [])
        assert "1 screen(s) added" in result.summary
        assert "1 screen(s) removed" in result.summary
        assert "1 screen(s) changed" in result.summary

    def test_summary_semicolon_separated(self) -> None:
        before = [_screen("s1", activity_name="com.app.Old")]
        after = [_screen("s2", activity_name="com.app.New")]
        result = compare_inspections(before, after, [], [])
        assert "; " in result.summary


# ---------------------------------------------------------------------------
# Tests: _compare_screens helper
# ---------------------------------------------------------------------------


class TestCompareScreensHelper:
    def test_identical_returns_none(self) -> None:
        s = _screen("s1", text_content="Hello")
        assert _compare_screens(s, s) is None

    def test_different_returns_screen_change(self) -> None:
        a = _screen("s1", screen_type="login", interactive_elements=3)
        b = _screen("s2", screen_type="form", interactive_elements=5)
        result = _compare_screens(a, b)
        assert isinstance(result, ScreenChange)
        assert result.screen_type_before == "login"
        assert result.screen_type_after == "form"

    def test_empty_text_both_sides_is_unchanged(self) -> None:
        a = _screen("s1", text_content=None)
        b = _screen("s2", text_content=None)
        # Same type and same elements, so no change
        result = _compare_screens(a, b)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _screen_similarity helper
# ---------------------------------------------------------------------------


class TestScreenSimilarityHelper:
    def test_identical_screens_high_similarity(self) -> None:
        s = _screen("s1", screen_type="login", text_content="Username Password")
        assert _screen_similarity(s, s) > 0.9

    def test_completely_different_low_similarity(self) -> None:
        a = _screen("s1", screen_type="login", text_content="AAAA")
        b = _screen("s2", screen_type="dashboard", text_content="ZZZZ")
        assert _screen_similarity(a, b) < 0.5

    def test_both_empty_text_gets_bonus(self) -> None:
        a = _screen("s1", screen_type="splash", text_content="")
        b = _screen("s2", screen_type="splash", text_content="")
        # type match (0.3) + empty text bonus (0.3) = 0.6
        assert _screen_similarity(a, b) >= 0.6

    def test_none_text_treated_as_empty(self) -> None:
        a = _screen("s1", screen_type="splash", text_content=None)
        b = _screen("s2", screen_type="splash", text_content=None)
        assert _screen_similarity(a, b) >= 0.6
