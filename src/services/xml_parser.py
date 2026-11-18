"""Parse Appium XML page source into structured data."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from lxml import etree

logger = logging.getLogger(__name__)

# Interactive element tag patterns (iOS + Android)
_INTERACTIVE_TAGS = {
    # iOS
    "XCUIElementTypeButton",
    "XCUIElementTypeTextField",
    "XCUIElementTypeSecureTextField",
    "XCUIElementTypeSwitch",
    "XCUIElementTypeSlider",
    "XCUIElementTypeLink",
    "XCUIElementTypeSearchField",
    # Android
    "android.widget.Button",
    "android.widget.EditText",
    "android.widget.CheckBox",
    "android.widget.RadioButton",
    "android.widget.Switch",
    "android.widget.ToggleButton",
    "android.widget.ImageButton",
    "android.widget.Spinner",
}


@dataclass
class ParsedElement:
    """A parsed interactive element from the page source."""

    tag: str
    text: str = ""
    content_desc: str = ""
    resource_id: str = ""
    bounds: str = ""
    clickable: bool = False
    enabled: bool = True


@dataclass
class ContentSection:
    """A content section detected on the screen."""

    heading: str = ""
    element_count: int = 0
    has_interactive: bool = False


@dataclass
class ParsedScreen:
    """Structured data from parsing an Appium XML page source."""

    interactive_elements: list[ParsedElement] = field(default_factory=list)
    text_content: str = ""
    accessibility_ids: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    word_count: int = 0
    has_forms: bool = False
    has_media: bool = False
    content_sections: list[ContentSection] = field(default_factory=list)


def parse_xml_source(xml_source: str) -> ParsedScreen:
    """Parse Appium XML page source into structured data."""
    if not xml_source or not xml_source.strip():
        return ParsedScreen()

    try:
        root = etree.fromstring(xml_source.encode("utf-8"))
    except etree.XMLSyntaxError:
        logger.warning("Failed to parse XML source")
        return ParsedScreen()

    elements: list[ParsedElement] = []
    texts: list[str] = []
    acc_ids: list[str] = []
    headings: list[str] = []

    for elem in root.iter():
        tag = elem.tag or ""

        # Extract text
        text = elem.get("text", "") or elem.get("value", "") or elem.get("label", "") or ""
        content_desc = elem.get("content-desc", "") or elem.get("name", "") or ""
        resource_id = elem.get("resource-id", "") or elem.get("accessibility-id", "") or ""

        if text:
            texts.append(text)
        if content_desc and content_desc not in texts:
            texts.append(content_desc)

        # Collect accessibility IDs
        acc_id = elem.get("accessibility-id", "") or elem.get("content-desc", "")
        if acc_id:
            acc_ids.append(acc_id)

        # Detect headings (large text elements near top)
        if text and len(text) < 100:
            bounds = elem.get("bounds", "")
            if bounds and _is_heading_position(bounds):
                headings.append(text)

        # Collect interactive elements
        clickable = elem.get("clickable", "false").lower() == "true"
        enabled = elem.get("enabled", "true").lower() != "false"

        if tag in _INTERACTIVE_TAGS or clickable:
            elements.append(
                ParsedElement(
                    tag=tag,
                    text=text,
                    content_desc=content_desc,
                    resource_id=resource_id,
                    bounds=elem.get("bounds", ""),
                    clickable=clickable,
                    enabled=enabled,
                )
            )

    all_text = " ".join(texts)

    # Form detection: presence of EditText/TextField elements
    has_forms = any(
        e.tag
        in {
            "android.widget.EditText",
            "XCUIElementTypeTextField",
            "XCUIElementTypeSecureTextField",
        }
        for e in elements
    )

    # Media detection
    media_tags = {
        "android.widget.ImageView",
        "android.widget.VideoView",
        "XCUIElementTypeImage",
        "XCUIElementTypeVideo",
    }
    has_media = any((elem.tag or "") in media_tags for elem in root.iter())

    return ParsedScreen(
        interactive_elements=elements,
        text_content=all_text,
        accessibility_ids=acc_ids,
        headings=headings,
        word_count=len(all_text.split()),
        has_forms=has_forms,
        has_media=has_media,
    )


def _is_heading_position(bounds: str) -> bool:
    """Check if bounds suggest a heading position (top portion of screen)."""
    # Android bounds format: [x1,y1][x2,y2]
    try:
        parts = bounds.replace("[", "").replace("]", ",").split(",")
        parts = [p for p in parts if p.strip()]
        if len(parts) >= 4:
            y1 = int(parts[1])
            return y1 < 400  # top portion of screen
    except ValueError, IndexError:
        pass
    return False
