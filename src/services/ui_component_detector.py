"""UI component detection for mobile app screens."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lxml import etree

logger = logging.getLogger(__name__)


@dataclass
class DetectedComponent:
    """A detected UI component."""

    component_type: str  # e.g. "bottom_navigation", "fab", "card", "list", "tab_layout"
    tag: str = ""
    confidence: float = 1.0
    resource_id: str = ""


# Tag-to-component mappings
_ANDROID_COMPONENTS: dict[str, str] = {
    "com.google.android.material.bottomnavigation.BottomNavigationView": "bottom_navigation",
    "com.google.android.material.navigation.NavigationView": "navigation_drawer",
    "com.google.android.material.tabs.TabLayout": "tab_layout",
    "com.google.android.material.floatingactionbutton.FloatingActionButton": "fab",
    "com.google.android.material.floatingactionbutton.ExtendedFloatingActionButton": "fab",
    "androidx.recyclerview.widget.RecyclerView": "list",
    "android.widget.ListView": "list",
    "androidx.cardview.widget.CardView": "card",
    "com.google.android.material.card.MaterialCardView": "card",
    "com.google.android.material.appbar.AppBarLayout": "app_bar",
    "com.google.android.material.appbar.CollapsingToolbarLayout": "collapsing_toolbar",
    "com.google.android.material.chip.ChipGroup": "chip_group",
    "com.google.android.material.bottomsheet.BottomSheetBehavior": "bottom_sheet",
    "android.widget.SearchView": "search_bar",
    "androidx.viewpager2.widget.ViewPager2": "view_pager",
    "androidx.viewpager.widget.ViewPager": "view_pager",
    "android.widget.ProgressBar": "progress_indicator",
    "android.widget.GridView": "grid",
    "android.widget.Toolbar": "toolbar",
    "androidx.appcompat.widget.Toolbar": "toolbar",
}

_IOS_COMPONENTS: dict[str, str] = {
    "UITabBar": "tab_bar",
    "UINavigationBar": "navigation_bar",
    "UICollectionView": "collection",
    "UITableView": "list",
    "UISearchBar": "search_bar",
    "UIToolbar": "toolbar",
    "UIPageControl": "page_control",
    "UIActivityIndicatorView": "progress_indicator",
    "UISegmentedControl": "segmented_control",
}

# Resource ID patterns for additional detection
_RESOURCE_ID_PATTERNS: dict[str, str] = {
    "bottom_nav": "bottom_navigation",
    "bottomNav": "bottom_navigation",
    "fab": "fab",
    "floating_action": "fab",
    "recycler": "list",
    "card_view": "card",
    "tab_layout": "tab_layout",
    "search": "search_bar",
    "toolbar": "toolbar",
    "drawer": "navigation_drawer",
}


def _match_tag(
    tag: str,
    resource_id: str,
    tag_map: dict[str, str],
    seen_types: set[str],
) -> DetectedComponent | None:
    """Return a high-confidence component if `tag` is in the tag map and unseen."""
    if tag in tag_map and tag_map[tag] not in seen_types:
        return DetectedComponent(
            component_type=tag_map[tag],
            tag=tag,
            confidence=1.0,
            resource_id=resource_id,
        )
    return None


def _match_class_attr(
    elem: etree._Element,
    resource_id: str,
    tag_map: dict[str, str],
    seen_types: set[str],
) -> DetectedComponent | None:
    """Return a high-confidence component derived from the `class` attribute."""
    class_name = elem.get("class", "")
    if class_name and class_name in tag_map and tag_map[class_name] not in seen_types:
        return DetectedComponent(
            component_type=tag_map[class_name],
            tag=class_name,
            confidence=1.0,
            resource_id=resource_id,
        )
    return None


def _match_resource_id(
    tag: str,
    resource_id: str,
    seen_types: set[str],
) -> list[DetectedComponent]:
    """Return lower-confidence components inferred from resource-id substrings."""
    if not resource_id:
        return []
    matches: list[DetectedComponent] = []
    rid_lower = resource_id.lower()
    for pattern, comp_type in _RESOURCE_ID_PATTERNS.items():
        if pattern in rid_lower and comp_type not in seen_types:
            matches.append(
                DetectedComponent(
                    component_type=comp_type,
                    tag=tag,
                    confidence=0.7,
                    resource_id=resource_id,
                )
            )
            seen_types.add(comp_type)
    return matches


def detect_ui_components(xml_source: str, platform: str = "android") -> list[DetectedComponent]:
    """Detect UI kit components from XML page source.

    Matches XML tags to Material Design (Android) and iOS HIG components.
    """
    if not xml_source or not xml_source.strip():
        return []
    try:
        root = etree.fromstring(xml_source.encode("utf-8"))
    except etree.XMLSyntaxError:
        return []

    components: list[DetectedComponent] = []
    seen_types: set[str] = set()
    tag_map = _ANDROID_COMPONENTS if platform != "ios" else _IOS_COMPONENTS

    for elem in root.iter():
        tag = elem.tag or ""
        resource_id = elem.get("resource-id", "") or elem.get("accessibility-id", "") or ""

        if (tag_match := _match_tag(tag, resource_id, tag_map, seen_types)) is not None:
            components.append(tag_match)
            seen_types.add(tag_match.component_type)

        if (class_match := _match_class_attr(elem, resource_id, tag_map, seen_types)) is not None:
            components.append(class_match)
            seen_types.add(class_match.component_type)

        components.extend(_match_resource_id(tag, resource_id, seen_types))

    return components
