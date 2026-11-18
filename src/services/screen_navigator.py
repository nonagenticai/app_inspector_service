"""BFS screen discovery for mobile apps via automation backends.

Traverses the app by tapping interactive elements and collecting
screen metadata, accessibility reports, and screenshots.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from selenium.common.exceptions import WebDriverException

from src.api.constants import DEFAULT_MAX_SCREENS
from src.services.accessibility_analyzer import analyze_accessibility as _real_analyze_accessibility
from src.services.automation_client import AutomationClient
from src.services.screen_classifier import classify_screen as _real_classify_screen
from src.services.ui_component_detector import detect_ui_components as _real_detect_ui_components
from src.services.xml_parser import parse_xml_source as _real_parse_xml_source

if TYPE_CHECKING:
    from src.services.accessibility_analyzer import AccessibilityReport
    from src.services.ui_component_detector import DetectedComponent
    from src.services.xml_parser import ParsedScreen

logger = logging.getLogger(__name__)

_MAX_DEPTH = 5

# Attributes that change between visits (ignore for dedup)
_DYNAMIC_ATTRS = re.compile(r'(bounds|instance|index|focused|selected|checked)="[^"]*"')


@dataclass
class DiscoveredScreen:
    """A discovered screen from BFS traversal."""

    screen_id: str
    screen_type: str
    xml_hash: str
    activity_name: str | None = None
    text_content: str = ""
    interactive_element_count: int = 0
    accessibility_ids: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    # Explicit capture outcome, so a screenshot-less screen is marked as such
    # rather than being indistinguishable from a captured one. See
    # ScreenInfo.screenshot_status.
    screenshot_status: str = "not_requested"
    headings: list[str] = field(default_factory=list)
    has_forms: bool = False
    has_media: bool = False
    content_sections: list[dict] = field(default_factory=list)
    accessibility_issues: list[dict] = field(default_factory=list)
    accessibility_score: int | None = None
    interactive_elements_detail: list[dict] = field(default_factory=list)
    ui_components: list[dict] = field(default_factory=list)
    # Tap/back attempts FROM this screen that raised and were dropped. Kept per
    # screen (not as a bare global) because that is where this repo already puts
    # a countable fact: the response model derives its aggregate from ``screens``
    # exactly as it does for ``screenshots_captured``. A dropped transition means
    # the outgoing edges of THIS screen are incomplete -- a 0-edge screen with
    # navigation_failures > 0 was never observed to have no navigation.
    navigation_failures: int = 0


@dataclass
class NavigationEdge:
    """Edge in the navigation graph."""

    from_screen: str
    to_screen: str
    action: str = "tap"
    element_label: str | None = None


# ---------------------------------------------------------------------------
# Collaborator Protocols (DI seams for testing)
# ---------------------------------------------------------------------------


class XMLParserProto(Protocol):
    """Parses XML page source into a ParsedScreen-like object."""

    def __call__(self, xml_source: str) -> ParsedScreen: ...


class ScreenClassifierProto(Protocol):
    """Classifies a screen into a screen-type string."""

    def __call__(
        self,
        xml_source: str,
        activity_name: str | None = None,
        text_content: str | None = None,
        accessibility_ids: list[str] | None = None,
    ) -> str: ...


class AccessibilityAnalyzerProto(Protocol):
    """Analyzes accessibility issues on a screen."""

    def __call__(self, xml_source: str, platform: str = "android") -> AccessibilityReport: ...


class UIComponentDetectorProto(Protocol):
    """Detects UI components (lists, FAB, tabs, etc.)."""

    def __call__(self, xml_source: str, platform: str = "android") -> list[DetectedComponent]: ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _xml_hash(xml_source: str) -> str:
    """Hash XML source ignoring dynamic attributes for dedup."""
    normalized = _DYNAMIC_ATTRS.sub("", xml_source)
    return hashlib.md5(normalized.encode(), usedforsecurity=False).hexdigest()


def _tap_point(elem: dict) -> tuple[int, int]:
    """Compute the (x, y) center of an element's bounding box."""
    loc = elem.get("location", {})
    size = elem.get("size", {})
    x = loc.get("x", 0) + size.get("width", 0) // 2
    y = loc.get("y", 0) + size.get("height", 0) // 2
    return x, y


def _element_label(elem: dict) -> str:
    """Extract a human-readable label from an element."""
    return elem.get("text", "") or elem.get("content_desc", "") or elem.get("resource_id", "")


async def _safe_back(client: AutomationClient) -> None:
    """Best-effort navigate-back; swallow OS/runtime errors."""
    try:
        await client.back()
    except (OSError, RuntimeError, WebDriverException) as exc:
        logger.warning("Failed to navigate back after error: %s", exc)


# ---------------------------------------------------------------------------
# ScreenNavigator -- the DI-friendly class form of discover_screens
# ---------------------------------------------------------------------------


class ScreenNavigator:
    """BFS screen discovery with injectable analyzer collaborators.

    Production callers use the defaults (real analyzers). Tests inject
    Protocol-conformant fakes via the constructor.
    """

    def __init__(
        self,
        *,
        xml_parser: XMLParserProto | None = None,
        screen_classifier: ScreenClassifierProto | None = None,
        accessibility_analyzer: AccessibilityAnalyzerProto | None = None,
        ui_component_detector: UIComponentDetectorProto | None = None,
    ) -> None:
        self._parse_xml: XMLParserProto = xml_parser or _real_parse_xml_source
        self._classify: ScreenClassifierProto = screen_classifier or _real_classify_screen
        self._analyze_accessibility: AccessibilityAnalyzerProto = (
            accessibility_analyzer or _real_analyze_accessibility
        )
        self._detect_ui: UIComponentDetectorProto = (
            ui_component_detector or _real_detect_ui_components
        )

    async def process_screen(
        self,
        xml_source: str,
        xml_hash: str,
        client: AutomationClient,
        screenshot_dir: str | None,
        screen_index: int,
    ) -> DiscoveredScreen:
        """Parse and classify a screen, optionally capturing a screenshot."""
        parsed = self._parse_xml(xml_source)
        screen_type = self._classify(
            xml_source,
            text_content=parsed.text_content,
            accessibility_ids=parsed.accessibility_ids,
        )
        acc_report = self._analyze_accessibility(xml_source)

        interactive_detail = [
            {
                "tag": e.tag,
                "text": e.text,
                "content_desc": e.content_desc,
                "resource_id": e.resource_id,
                "clickable": e.clickable,
            }
            for e in parsed.interactive_elements[:50]
        ]

        content_sections = [
            {
                "heading": cs.heading,
                "element_count": cs.element_count,
                "has_interactive": cs.has_interactive,
            }
            for cs in getattr(parsed, "content_sections", [])
        ]

        ui_components = [
            {
                "component_type": c.component_type,
                "tag": c.tag,
                "confidence": c.confidence,
                "resource_id": c.resource_id,
            }
            for c in self._detect_ui(xml_source)
        ]

        screenshot_path: str | None = None
        screenshot_status = "not_requested"
        if screenshot_dir:
            ss_path = Path(screenshot_dir) / f"screen_{screen_index}.png"
            screenshot_status = "failed"
            try:
                # Take the path the client RETURNS, never the one we asked for:
                # the return value is the client's assertion that a readable
                # file landed there. Pre-assigning it made a failed capture
                # indistinguishable from a successful one.
                screenshot_path = await client.screenshot(str(ss_path))
            except (OSError, RuntimeError, WebDriverException) as exc:
                logger.warning("Failed to capture screenshot for screen_%d: %s", screen_index, exc)
                screenshot_path = None
            else:
                if screenshot_path:
                    screenshot_status = "captured"
                else:
                    logger.warning("Screenshot client returned no path for screen_%d", screen_index)
                    screenshot_path = None

        return DiscoveredScreen(
            screen_id=f"screen_{screen_index}",
            screen_type=screen_type,
            xml_hash=xml_hash,
            text_content=parsed.text_content[:1000] if parsed.text_content else "",
            interactive_element_count=len(parsed.interactive_elements),
            accessibility_ids=parsed.accessibility_ids[:50],
            screenshot_path=screenshot_path,
            screenshot_status=screenshot_status,
            headings=parsed.headings[:20],
            has_forms=parsed.has_forms,
            has_media=parsed.has_media,
            content_sections=content_sections,
            accessibility_issues=[
                {
                    "rule": i.rule,
                    "severity": i.severity,
                    "element_tag": i.element_tag,
                    "element_text": i.element_text,
                    "message": i.message,
                }
                for i in acc_report.issues
            ],
            accessibility_score=acc_report.score,
            interactive_elements_detail=interactive_detail,
            ui_components=ui_components,
        )

    async def _explore_element(
        self,
        client: AutomationClient,
        elem: dict,
        *,
        current_hash: str,
        depth: int,
        screens: dict[str, DiscoveredScreen],
        edges: list[NavigationEdge],
        queue: deque[tuple[str, int]],
        visited_transitions: set[tuple[str, str]],
        screenshot_dir: str | None,
        on_screen_discovered: Callable[[DiscoveredScreen], None] | None,
    ) -> None:
        """Tap one element, record any new screen/edge, then navigate back."""
        import asyncio

        x, y = _tap_point(elem)
        label = _element_label(elem)
        try:
            await client.tap(x, y)
            await asyncio.sleep(1.0)

            new_xml = await client.get_page_source()
            new_hash = _xml_hash(new_xml)
            if new_hash == current_hash:
                return

            edges.append(
                NavigationEdge(
                    from_screen=current_hash,
                    to_screen=new_hash,
                    action="tap",
                    element_label=label[:200] if label else None,
                )
            )

            transition = (current_hash, new_hash)
            if new_hash not in screens and transition not in visited_transitions:
                new_screen = await self.process_screen(
                    new_xml, new_hash, client, screenshot_dir, len(screens)
                )
                screens[new_hash] = new_screen
                if on_screen_discovered:
                    on_screen_discovered(new_screen)
                queue.append((new_hash, depth + 1))

            visited_transitions.add(transition)
            await client.back()
            await asyncio.sleep(0.5)
        except (OSError, RuntimeError, WebDriverException) as exc:
            # COUNT the drop, don't only log it. A swallowed tap/back leaves no
            # edge behind, so downstream a partially-explored screen is
            # byte-identical to a screen that genuinely has nowhere to go --
            # which is how purpose inference came to describe a 0-edge graph as
            # having "simple, linear navigation" it had never observed.
            source = screens.get(current_hash)
            if source is not None:
                source.navigation_failures += 1
            logger.warning(
                "Failed to explore element %s from screen %s (dropped, "
                "navigation graph is now incomplete for this screen): %s",
                label[:50] if label else "?",
                current_hash[:12],
                exc,
            )
            await _safe_back(client)

    async def discover(
        self,
        client: AutomationClient,
        max_screens: int = DEFAULT_MAX_SCREENS,
        screenshot_dir: str | None = None,
        on_screen_discovered: Callable[[DiscoveredScreen], None] | None = None,
    ) -> tuple[list[DiscoveredScreen], list[NavigationEdge]]:
        """BFS screen discovery through the app.

        Returns (screens, navigation_edges).
        """
        screens: dict[str, DiscoveredScreen] = {}
        edges: list[NavigationEdge] = []
        queue: deque[tuple[str, int]] = deque()
        visited_transitions: set[tuple[str, str]] = set()

        xml = await client.get_page_source()
        initial_hash = _xml_hash(xml)
        initial_screen = await self.process_screen(
            xml, initial_hash, client, screenshot_dir, len(screens)
        )
        screens[initial_hash] = initial_screen
        if on_screen_discovered:
            on_screen_discovered(initial_screen)
        queue.append((initial_hash, 0))

        while queue and len(screens) < max_screens:
            current_hash, depth = queue.popleft()
            if depth >= _MAX_DEPTH:
                continue

            clickable = await client.find_clickable_elements()
            if not clickable:
                continue

            for elem in clickable[:10]:
                if len(screens) >= max_screens:
                    break
                await self._explore_element(
                    client,
                    elem,
                    current_hash=current_hash,
                    depth=depth,
                    screens=screens,
                    edges=edges,
                    queue=queue,
                    visited_transitions=visited_transitions,
                    screenshot_dir=screenshot_dir,
                    on_screen_discovered=on_screen_discovered,
                )

        return list(screens.values()), edges


# ---------------------------------------------------------------------------
# Backward-compatible free-function API
# ---------------------------------------------------------------------------


async def discover_screens(
    client: AutomationClient,
    max_screens: int = DEFAULT_MAX_SCREENS,
    screenshot_dir: str | None = None,
    on_screen_discovered: Callable[[DiscoveredScreen], None] | None = None,
) -> tuple[list[DiscoveredScreen], list[NavigationEdge]]:
    """BFS screen discovery (free-function form, uses real default analyzers)."""
    navigator = ScreenNavigator()
    return await navigator.discover(
        client,
        max_screens=max_screens,
        screenshot_dir=screenshot_dir,
        on_screen_discovered=on_screen_discovered,
    )


async def _process_screen(
    xml_source: str,
    xml_hash: str,
    client: AutomationClient,
    screenshot_dir: str | None,
    screen_index: int,
) -> DiscoveredScreen:
    """Backward-compatible private helper. Prefer ScreenNavigator.process_screen()."""
    return await ScreenNavigator().process_screen(
        xml_source, xml_hash, client, screenshot_dir, screen_index
    )


# Re-export for any external caller doing module-level patching during transition.
__all__ = [
    "AccessibilityAnalyzerProto",
    "DiscoveredScreen",
    "NavigationEdge",
    "ScreenClassifierProto",
    "ScreenNavigator",
    "UIComponentDetectorProto",
    "XMLParserProto",
    "discover_screens",
]


# Avoid unused-import warnings via TYPE_CHECKING-only references.
_ = (Any,)
