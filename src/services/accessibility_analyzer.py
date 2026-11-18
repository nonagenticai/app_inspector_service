"""Accessibility analysis for mobile app screens."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from lxml import etree

logger = logging.getLogger(__name__)

# Minimum touch target sizes (in dp/pt)
_MIN_TOUCH_SIZE_ANDROID = 48
_MIN_TOUCH_SIZE_IOS = 44


@dataclass
class AccessibilityIssue:
    """A single accessibility issue found on a screen."""

    rule: str
    severity: str  # "error", "warning", "info"
    element_tag: str = ""
    element_text: str = ""
    element_resource_id: str = ""
    message: str = ""


@dataclass
class AccessibilityReport:
    """Accessibility analysis result for a single screen."""

    issues: list[AccessibilityIssue] = field(default_factory=list)
    # None == NOT MEASURED (blank/malformed page source, or no scoreable
    # element on the screen). Never default this to 100: a screen that could
    # not be analysed is not a screen that passed.
    score: int | None = None
    total_elements_checked: int = 0
    elements_with_issues: int = 0


_INPUT_TAGS = frozenset(
    {
        "android.widget.EditText",
        "XCUIElementTypeTextField",
        "XCUIElementTypeSecureTextField",
        "android.widget.SearchView",
    }
)


@dataclass
class _ElementCtx:
    """Resolved attributes for one element under inspection."""

    tag: str
    clickable: bool
    is_input: bool
    text: str
    content_desc: str
    resource_id: str
    bounds: str
    elem_id: str


def _read_element(elem: etree._Element) -> _ElementCtx:
    tag = elem.tag or ""
    clickable = elem.get("clickable", "false").lower() == "true"
    is_input = tag in _INPUT_TAGS
    text = elem.get("text", "") or elem.get("label", "") or ""
    content_desc = elem.get("content-desc", "") or elem.get("name", "") or ""
    resource_id = elem.get("resource-id", "") or elem.get("accessibility-id", "") or ""
    bounds = elem.get("bounds", "")
    elem_id = f"{tag}:{resource_id or text or bounds}"
    return _ElementCtx(
        tag=tag,
        clickable=clickable,
        is_input=is_input,
        text=text,
        content_desc=content_desc,
        resource_id=resource_id,
        bounds=bounds,
        elem_id=elem_id,
    )


def _check_missing_content_desc(ctx: _ElementCtx) -> AccessibilityIssue | None:
    if ctx.clickable and not ctx.content_desc and not ctx.text:
        return AccessibilityIssue(
            rule="missing_content_description",
            severity="error",
            element_tag=ctx.tag,
            element_text=ctx.text,
            element_resource_id=ctx.resource_id,
            message=f"Clickable element '{ctx.tag}' has no text or content description",
        )
    return None


def _check_small_touch(ctx: _ElementCtx, min_size: int) -> AccessibilityIssue | None:
    if not (ctx.clickable and ctx.bounds):
        return None
    width, height = _parse_touch_size(ctx.bounds)
    if 0 < width < min_size or 0 < height < min_size:
        return AccessibilityIssue(
            rule="small_touch_target",
            severity="warning",
            element_tag=ctx.tag,
            element_text=ctx.text,
            element_resource_id=ctx.resource_id,
            message=(
                f"Touch target too small ({width}x{height}dp), minimum is {min_size}x{min_size}dp"
            ),
        )
    return None


def _check_missing_input_label(ctx: _ElementCtx) -> AccessibilityIssue | None:
    if ctx.is_input and not ctx.text and not ctx.content_desc:
        return AccessibilityIssue(
            rule="missing_input_label",
            severity="error",
            element_tag=ctx.tag,
            element_text=ctx.text,
            element_resource_id=ctx.resource_id,
            message=f"Input field '{ctx.tag}' has no label or hint text",
        )
    return None


def _check_duplicate_acc_id(
    elem: etree._Element, ctx: _ElementCtx, acc_id_counts: dict[str, int]
) -> AccessibilityIssue | None:
    acc_id = elem.get("accessibility-id", "") or elem.get("content-desc", "")
    if acc_id and acc_id_counts.get(acc_id, 0) > 1:
        return AccessibilityIssue(
            rule="duplicate_accessibility_id",
            severity="warning",
            element_tag=ctx.tag,
            element_text=ctx.text,
            element_resource_id=ctx.resource_id,
            message=f"Duplicate accessibility ID: '{acc_id}'",
        )
    return None


def _check_nested_clickable(
    root: etree._Element,
) -> list[tuple[AccessibilityIssue, str]]:
    """Return (issue, elem_id) pairs for clickable elements nested inside other clickables."""
    out: list[tuple[AccessibilityIssue, str]] = []
    for elem in root.iter():
        if elem.get("clickable", "false").lower() != "true":
            continue
        for child in elem.iter():
            if child is elem:
                continue
            if child.get("clickable", "false").lower() == "true":
                child_tag = child.tag or ""
                child_text = child.get("text", "") or ""
                child_rid = child.get("resource-id", "") or ""
                issue = AccessibilityIssue(
                    rule="nested_clickable",
                    severity="warning",
                    element_tag=child_tag,
                    element_text=child_text,
                    element_resource_id=child_rid,
                    message="Clickable element nested inside another clickable element",
                )
                out.append((issue, f"{child_tag}:{child_rid or child_text}"))
                break  # Only report once per parent
    return out


def _count_acc_ids(root: etree._Element) -> dict[str, int]:
    counts: dict[str, int] = {}
    for elem in root.iter():
        acc_id = elem.get("accessibility-id", "") or elem.get("content-desc", "")
        if acc_id:
            counts[acc_id] = counts.get(acc_id, 0) + 1
    return counts


def _score(issues: list[AccessibilityIssue], total_checked: int) -> int | None:
    """Return the 0-100 score, or None when there was nothing to measure."""
    if total_checked <= 0:
        return None
    error_count = sum(1 for i in issues if i.severity == "error")
    warning_count = sum(1 for i in issues if i.severity == "warning")
    penalty = (error_count * 15) + (warning_count * 5)
    return max(0, 100 - penalty)


def analyze_accessibility(
    xml_source: str,
    platform: str = "android",
) -> AccessibilityReport:
    """Analyze a screen's XML source for accessibility issues.

    Returns an AccessibilityReport with issues and a 0-100 score.
    """
    if not xml_source or not xml_source.strip():
        return AccessibilityReport()

    try:
        root = etree.fromstring(xml_source.encode("utf-8"))
    except etree.XMLSyntaxError:
        return AccessibilityReport()

    issues: list[AccessibilityIssue] = []
    total_checked = 0
    elements_with_issues_set: set[str] = set()
    min_size = _MIN_TOUCH_SIZE_IOS if platform == "ios" else _MIN_TOUCH_SIZE_ANDROID
    acc_id_counts = _count_acc_ids(root)

    for elem in root.iter():
        ctx = _read_element(elem)
        if not ctx.clickable and not ctx.is_input:
            continue
        total_checked += 1

        for issue in (
            _check_missing_content_desc(ctx),
            _check_small_touch(ctx, min_size),
            _check_missing_input_label(ctx),
            _check_duplicate_acc_id(elem, ctx, acc_id_counts),
        ):
            if issue is not None:
                issues.append(issue)
                elements_with_issues_set.add(ctx.elem_id)

    for issue, elem_id in _check_nested_clickable(root):
        issues.append(issue)
        elements_with_issues_set.add(elem_id)

    return AccessibilityReport(
        issues=issues,
        score=_score(issues, total_checked),
        total_elements_checked=total_checked,
        elements_with_issues=len(elements_with_issues_set),
    )


def _parse_touch_size(bounds: str) -> tuple[int, int]:
    """Parse bounds string and return (width, height) in dp."""
    try:
        parts = bounds.replace("[", "").replace("]", ",").split(",")
        parts = [p for p in parts if p.strip()]
        if len(parts) >= 4:
            x1, y1 = int(parts[0]), int(parts[1])
            x2, y2 = int(parts[2]), int(parts[3])
            return abs(x2 - x1), abs(y2 - y1)
    except ValueError, IndexError:
        pass
    return 0, 0
