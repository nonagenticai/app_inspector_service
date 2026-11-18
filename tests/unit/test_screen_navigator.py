"""Unit tests for BFS screen discovery and ScreenNavigator.

Uses Protocol-conforming fakes injected into ``ScreenNavigator`` instead
of ``mock.patch``-ing the SUT module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.services.automation_client import ScreenshotCaptureError
from src.services.screen_navigator import (
    DiscoveredScreen,
    NavigationEdge,
    ScreenNavigator,
    _xml_hash,
)

# ---------------------------------------------------------------------------
# Fake collaborators (Protocol-conforming)
# ---------------------------------------------------------------------------


@dataclass
class _FakeParsedElement:
    tag: str = ""
    text: str = ""
    content_desc: str = ""
    resource_id: str = ""
    clickable: bool = False


@dataclass
class _FakeContentSection:
    heading: str = ""
    element_count: int = 0
    has_interactive: bool = False


@dataclass
class _FakeParsedScreen:
    text_content: str = ""
    interactive_elements: list[_FakeParsedElement] = field(default_factory=list)
    accessibility_ids: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    has_forms: bool = False
    has_media: bool = False
    content_sections: list[_FakeContentSection] = field(default_factory=list)


@dataclass
class _FakeAccessibilityIssue:
    rule: str = ""
    severity: str = "info"
    element_tag: str = ""
    element_text: str = ""
    message: str = ""


@dataclass
class _FakeAccessibilityReport:
    issues: list[_FakeAccessibilityIssue] = field(default_factory=list)
    score: int = 100


@dataclass
class _FakeDetectedComponent:
    component_type: str = ""
    tag: str = ""
    confidence: float = 1.0
    resource_id: str = ""


class _FakeXMLParser:
    """Fake XMLParserProto. Records each call; returns a configured ParsedScreen."""

    def __init__(self, parsed: _FakeParsedScreen | None = None) -> None:
        self.parsed: _FakeParsedScreen = parsed or _FakeParsedScreen()
        self.calls: list[str] = []

    def __call__(self, xml_source: str) -> Any:
        self.calls.append(xml_source)
        return self.parsed


class _FakeScreenClassifier:
    def __init__(self, returns: str = "unknown") -> None:
        self.returns = returns
        self.calls: list[tuple[str, dict]] = []

    def __call__(
        self,
        xml_source: str,
        activity_name: str | None = None,
        text_content: str | None = None,
        accessibility_ids: list[str] | None = None,
    ) -> str:
        self.calls.append(
            (
                xml_source,
                {
                    "activity_name": activity_name,
                    "text_content": text_content,
                    "accessibility_ids": accessibility_ids,
                },
            )
        )
        return self.returns


class _FakeAccessibilityAnalyzer:
    def __init__(self, report: _FakeAccessibilityReport | None = None) -> None:
        self.report: _FakeAccessibilityReport = report or _FakeAccessibilityReport()
        self.calls: list[str] = []

    def __call__(self, xml_source: str, platform: str = "android") -> Any:
        self.calls.append(xml_source)
        return self.report


class _FakeUIComponentDetector:
    def __init__(self, components: list[_FakeDetectedComponent] | None = None) -> None:
        self.components: list[_FakeDetectedComponent] = components or []
        self.calls: list[str] = []

    def __call__(self, xml_source: str, platform: str = "android") -> Any:
        self.calls.append(xml_source)
        return self.components


def _make_navigator(
    *,
    parsed: _FakeParsedScreen | None = None,
    screen_type: str = "unknown",
    acc_report: _FakeAccessibilityReport | None = None,
    components: list[_FakeDetectedComponent] | None = None,
) -> tuple[
    ScreenNavigator,
    _FakeXMLParser,
    _FakeScreenClassifier,
    _FakeAccessibilityAnalyzer,
    _FakeUIComponentDetector,
]:
    xml_parser = _FakeXMLParser(parsed=parsed)
    classifier = _FakeScreenClassifier(returns=screen_type)
    analyzer = _FakeAccessibilityAnalyzer(report=acc_report)
    detector = _FakeUIComponentDetector(components=components)
    nav = ScreenNavigator(
        xml_parser=xml_parser,
        screen_classifier=classifier,
        accessibility_analyzer=analyzer,
        ui_component_detector=detector,
    )
    return nav, xml_parser, classifier, analyzer, detector


# ============================================================================
# _xml_hash (pure function, no DI required)
# ============================================================================


class TestXmlHash:
    def test_same_content_same_hash(self):
        xml = "<hierarchy><button text='A' /></hierarchy>"
        assert _xml_hash(xml) == _xml_hash(xml)

    def test_ignores_bounds(self):
        xml1 = '<hierarchy bounds="[0,0][100,200]" />'
        xml2 = '<hierarchy bounds="[50,50][200,400]" />'
        assert _xml_hash(xml1) == _xml_hash(xml2)

    def test_ignores_instance(self):
        xml1 = '<hierarchy instance="0" />'
        xml2 = '<hierarchy instance="5" />'
        assert _xml_hash(xml1) == _xml_hash(xml2)

    def test_ignores_focused(self):
        xml1 = '<button focused="true" />'
        xml2 = '<button focused="false" />'
        assert _xml_hash(xml1) == _xml_hash(xml2)

    def test_different_content_different_hash(self):
        xml1 = "<hierarchy><button text='Login' /></hierarchy>"
        xml2 = "<hierarchy><button text='Register' /></hierarchy>"
        assert _xml_hash(xml1) != _xml_hash(xml2)


# ============================================================================
# ScreenNavigator.process_screen
# ============================================================================


class TestProcessScreen:
    async def test_process_screen_basic(self):
        parsed = _FakeParsedScreen(
            text_content="Hello World",
            accessibility_ids=["btn1"],
            headings=["Welcome"],
        )
        nav, xml_parser, classifier, analyzer, _ = _make_navigator(
            parsed=parsed,
            screen_type="login",
            acc_report=_FakeAccessibilityReport(score=95),
        )

        client = AsyncMock()
        screen = await nav.process_screen("<xml/>", "hash123", client, None, 0)

        assert isinstance(screen, DiscoveredScreen)
        assert screen.screen_id == "screen_0"
        assert screen.screen_type == "login"
        assert screen.xml_hash == "hash123"
        assert screen.text_content == "Hello World"
        assert screen.accessibility_score == 95
        assert screen.screenshot_path is None
        # Fakes recorded the call
        assert xml_parser.calls == ["<xml/>"]
        assert classifier.calls[0][1]["text_content"] == "Hello World"
        assert classifier.calls[0][1]["accessibility_ids"] == ["btn1"]
        assert analyzer.calls == ["<xml/>"]

    async def test_process_screen_with_screenshot_dir(self):
        """A path is carried only because the CLIENT returned one."""
        nav, *_ = _make_navigator()
        client = AsyncMock()
        client.screenshot = AsyncMock(return_value="/tmp/screenshots/screen_2.png")

        screen = await nav.process_screen("<xml/>", "hash456", client, "/tmp/screenshots", 2)

        assert screen.screenshot_path == "/tmp/screenshots/screen_2.png"
        assert screen.screenshot_status == "captured"
        client.screenshot.assert_awaited_once_with("/tmp/screenshots/screen_2.png")

    async def test_process_screen_screenshot_failure_carries_no_path(self):
        """A raising client must not leave a path to a file that never landed."""
        nav, *_ = _make_navigator()
        client = AsyncMock()
        client.screenshot = AsyncMock(side_effect=ScreenshotCaptureError("device error"))

        screen = await nav.process_screen("<xml/>", "hash456", client, "/tmp/screenshots", 2)

        assert screen.screenshot_path is None
        # The screen is still returned (the XML/a11y analysis is real value),
        # but it is EXPLICITLY marked as artifact-less rather than looking
        # identical to a screen that was captured.
        assert screen.screenshot_status == "failed"

    async def test_process_screen_screenshot_oserror_carries_no_path(self):
        nav, *_ = _make_navigator()
        client = AsyncMock()
        client.screenshot = AsyncMock(side_effect=OSError("device error"))

        screen = await nav.process_screen("<xml/>", "hash456", client, "/tmp/screenshots", 2)

        assert screen.screenshot_path is None
        assert screen.screenshot_status == "failed"

    async def test_process_screen_client_returning_no_path_is_not_success(self):
        """A client that returns nothing must not be read as a capture."""
        nav, *_ = _make_navigator()
        client = AsyncMock()
        client.screenshot = AsyncMock(return_value=None)

        screen = await nav.process_screen("<xml/>", "hash456", client, "/tmp/screenshots", 2)

        assert screen.screenshot_path is None
        assert screen.screenshot_status == "failed"

    async def test_process_screen_without_screenshot_dir_is_not_requested(self):
        nav, *_ = _make_navigator()
        client = AsyncMock()

        screen = await nav.process_screen("<xml/>", "hash456", client, None, 2)

        assert screen.screenshot_path is None
        assert screen.screenshot_status == "not_requested"
        client.screenshot.assert_not_awaited()

    async def test_process_screen_with_interactive_elements(self):
        elem = _FakeParsedElement(
            tag="android.widget.Button",
            text="Submit",
            content_desc="Submit form",
            resource_id="btn_submit",
            clickable=True,
        )
        parsed = _FakeParsedScreen(
            text_content="Submit form",
            interactive_elements=[elem],
            accessibility_ids=["btn_submit"],
            has_forms=True,
        )
        nav, *_ = _make_navigator(parsed=parsed, screen_type="form")

        client = AsyncMock()
        screen = await nav.process_screen("<xml/>", "hash789", client, None, 1)

        assert screen.interactive_element_count == 1
        assert len(screen.interactive_elements_detail) == 1
        assert screen.interactive_elements_detail[0]["tag"] == "android.widget.Button"
        assert screen.interactive_elements_detail[0]["clickable"] is True
        assert screen.has_forms is True

    async def test_process_screen_with_accessibility_issues(self):
        issue = _FakeAccessibilityIssue(
            rule="missing_content_description",
            severity="warning",
            element_tag="ImageView",
            element_text="",
            message="Missing content description",
        )
        nav, *_ = _make_navigator(
            screen_type="content",
            acc_report=_FakeAccessibilityReport(issues=[issue], score=70),
        )
        client = AsyncMock()
        screen = await nav.process_screen("<xml/>", "hashABC", client, None, 0)

        assert len(screen.accessibility_issues) == 1
        assert screen.accessibility_issues[0]["rule"] == "missing_content_description"
        assert screen.accessibility_issues[0]["severity"] == "warning"
        assert screen.accessibility_score == 70

    async def test_process_screen_with_ui_components(self):
        component = _FakeDetectedComponent(
            component_type="text_input",
            tag="EditText",
            confidence=0.95,
            resource_id="email_field",
        )
        nav, *_, detector = _make_navigator(
            screen_type="form",
            components=[component],
        )

        client = AsyncMock()
        screen = await nav.process_screen("<xml/>", "hashDEF", client, None, 0)

        assert len(screen.ui_components) == 1
        assert screen.ui_components[0]["component_type"] == "text_input"
        assert screen.ui_components[0]["confidence"] == 0.95
        assert detector.calls == ["<xml/>"]


# ============================================================================
# ScreenNavigator.discover (BFS)
# ============================================================================


class TestDiscoverScreens:
    async def test_discover_initial_screen_only(self):
        """When there are no clickable elements, only the initial screen is returned."""
        nav, *_ = _make_navigator(screen_type="home")
        client = AsyncMock()
        client.get_page_source = AsyncMock(return_value="<hierarchy><button text='A'/></hierarchy>")
        client.find_clickable_elements = AsyncMock(return_value=[])

        screens, edges = await nav.discover(client, max_screens=5)

        assert len(screens) == 1
        assert screens[0].screen_id == "screen_0"
        assert screens[0].screen_type == "home"
        assert edges == []

    async def test_discover_screens_with_navigation(self):
        """Clicking an element that leads to a new screen records an edge."""
        nav, *_ = _make_navigator(screen_type="home")
        client = AsyncMock()

        xml_initial = "<hierarchy><button text='Login' /></hierarchy>"
        xml_new = "<hierarchy><button text='Home' /></hierarchy>"

        client.get_page_source = AsyncMock(side_effect=[xml_initial, xml_new, xml_initial])
        client.find_clickable_elements = AsyncMock(
            side_effect=[
                [
                    {
                        "text": "Login",
                        "content_desc": "",
                        "resource_id": "btn_login",
                        "location": {"x": 10, "y": 20},
                        "size": {"width": 100, "height": 50},
                    }
                ],
                [],  # No clickable elements on new screen
            ]
        )
        client.tap = AsyncMock()
        client.back = AsyncMock()

        screens, edges = await nav.discover(client, max_screens=5)

        assert len(screens) == 2
        # initial hash and new hash come from the real _xml_hash
        assert screens[0].xml_hash == _xml_hash(xml_initial)
        assert screens[1].xml_hash == _xml_hash(xml_new)
        assert len(edges) == 1
        assert edges[0].action == "tap"
        assert edges[0].element_label == "Login"
        assert edges[0].from_screen == _xml_hash(xml_initial)
        assert edges[0].to_screen == _xml_hash(xml_new)

    async def test_max_screens_limit(self):
        """Discovery stops when max_screens is reached."""
        nav, *_ = _make_navigator(screen_type="home")
        client = AsyncMock()
        client.get_page_source = AsyncMock(return_value="<hierarchy/>")
        client.find_clickable_elements = AsyncMock(return_value=[])

        screens, _edges = await nav.discover(client, max_screens=1)

        assert len(screens) == 1

    async def test_on_screen_discovered_callback(self):
        """The callback is called for each discovered screen."""
        nav, *_ = _make_navigator(screen_type="home")
        client = AsyncMock()
        client.get_page_source = AsyncMock(return_value="<hierarchy/>")
        client.find_clickable_elements = AsyncMock(return_value=[])

        callback = MagicMock()
        screens, _ = await nav.discover(client, max_screens=5, on_screen_discovered=callback)

        callback.assert_called_once()
        # The callback receives the actual DiscoveredScreen returned
        assert callback.call_args.args[0] is screens[0]

    async def test_element_tap_error_handled(self):
        """Errors during tap/navigation are caught and don't crash discovery."""
        nav, *_ = _make_navigator(screen_type="unknown")
        client = AsyncMock()
        client.get_page_source = AsyncMock(return_value="<hierarchy/>")
        client.find_clickable_elements = AsyncMock(
            return_value=[
                {
                    "text": "Broken",
                    "content_desc": "",
                    "resource_id": "",
                    "location": {"x": 0, "y": 0},
                    "size": {"width": 0, "height": 0},
                }
            ]
        )
        client.tap = AsyncMock(side_effect=OSError("tap failed"))
        client.back = AsyncMock()

        screens, edges = await nav.discover(client, max_screens=5)

        assert len(screens) == 1
        assert edges == []
        # back() was attempted as recovery
        client.back.assert_awaited()

    async def test_element_tap_webdriver_exception_handled(self):
        """A Selenium/Appium WebDriverException (e.g. a stale element mid-crawl)
        is swallowed so a routine driver hiccup can't abort the whole inspection."""
        from selenium.common.exceptions import StaleElementReferenceException

        nav, *_ = _make_navigator(screen_type="unknown")
        client = AsyncMock()
        client.get_page_source = AsyncMock(return_value="<hierarchy/>")
        client.find_clickable_elements = AsyncMock(
            return_value=[
                {
                    "text": "Stale",
                    "content_desc": "",
                    "resource_id": "",
                    "location": {"x": 0, "y": 0},
                    "size": {"width": 0, "height": 0},
                }
            ]
        )
        client.tap = AsyncMock(side_effect=StaleElementReferenceException("element went stale"))
        client.back = AsyncMock()

        screens, edges = await nav.discover(client, max_screens=5)

        # Discovery completes with the initial screen instead of raising.
        assert len(screens) == 1
        assert edges == []
        client.back.assert_awaited()


# ============================================================================
# Dataclass tests
# ============================================================================


class TestDataclasses:
    def test_discovered_screen_defaults(self):
        screen = DiscoveredScreen(screen_id="s0", screen_type="home", xml_hash="abc")
        assert screen.activity_name is None
        assert screen.text_content == ""
        assert screen.interactive_element_count == 0
        assert screen.accessibility_ids == []
        assert screen.screenshot_path is None
        assert screen.headings == []
        assert screen.has_forms is False
        assert screen.has_media is False
        assert screen.accessibility_score is None  # absent != a perfect score

    def test_navigation_edge_defaults(self):
        edge = NavigationEdge(from_screen="s0", to_screen="s1")
        assert edge.action == "tap"
        assert edge.element_label is None
