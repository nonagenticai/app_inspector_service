"""Unit tests for ui_component_detector."""

from __future__ import annotations

import unittest

from src.services.ui_component_detector import (
    DetectedComponent,
    detect_ui_components,
)


class TestEmptyAndInvalidInput(unittest.TestCase):
    """Tests for empty/invalid XML input."""

    def test_empty_string_returns_empty(self):
        result = detect_ui_components("")
        self.assertEqual(result, [])

    def test_whitespace_only_returns_empty(self):
        result = detect_ui_components("   \n  ")
        self.assertEqual(result, [])

    def test_invalid_xml_returns_empty(self):
        result = detect_ui_components("<not<<valid")
        self.assertEqual(result, [])

    def test_xml_with_no_known_components(self):
        xml = '<hierarchy><android.widget.TextView text="Hello" /></hierarchy>'
        result = detect_ui_components(xml)
        self.assertEqual(result, [])


class TestAndroidMaterialComponents(unittest.TestCase):
    """Tests for Android Material Design component detection."""

    def test_bottom_navigation_view(self):
        xml = (
            "<hierarchy>"
            "<com.google.android.material.bottomnavigation"
            '.BottomNavigationView resource-id="bottom_nav" />'
            "</hierarchy>"
        )
        result = detect_ui_components(xml, platform="android")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].component_type, "bottom_navigation")
        self.assertEqual(result[0].confidence, 1.0)
        self.assertEqual(result[0].resource_id, "bottom_nav")

    def test_floating_action_button(self):
        xml = (
            "<hierarchy>"
            "<com.google.android.material.floatingactionbutton"
            '.FloatingActionButton resource-id="fab_add" />'
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("fab", types)

    def test_recycler_view(self):
        xml = "<hierarchy><androidx.recyclerview.widget.RecyclerView /></hierarchy>"
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("list", types)

    def test_card_view(self):
        xml = "<hierarchy><com.google.android.material.card.MaterialCardView /></hierarchy>"
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("card", types)

    def test_tab_layout(self):
        xml = "<hierarchy><com.google.android.material.tabs.TabLayout /></hierarchy>"
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("tab_layout", types)

    def test_navigation_drawer(self):
        xml = "<hierarchy><com.google.android.material.navigation.NavigationView /></hierarchy>"
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("navigation_drawer", types)

    def test_toolbar(self):
        xml = "<hierarchy><androidx.appcompat.widget.Toolbar /></hierarchy>"
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("toolbar", types)

    def test_search_view(self):
        xml = "<hierarchy><android.widget.SearchView /></hierarchy>"
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("search_bar", types)

    def test_progress_bar(self):
        xml = "<hierarchy><android.widget.ProgressBar /></hierarchy>"
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("progress_indicator", types)


class TestIOSComponents(unittest.TestCase):
    """Tests for iOS HIG component detection."""

    def test_tab_bar(self):
        xml = "<hierarchy><UITabBar /></hierarchy>"
        result = detect_ui_components(xml, platform="ios")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].component_type, "tab_bar")
        self.assertEqual(result[0].confidence, 1.0)

    def test_navigation_bar(self):
        xml = "<hierarchy><UINavigationBar /></hierarchy>"
        result = detect_ui_components(xml, platform="ios")
        types = [c.component_type for c in result]
        self.assertIn("navigation_bar", types)

    def test_table_view(self):
        xml = "<hierarchy><UITableView /></hierarchy>"
        result = detect_ui_components(xml, platform="ios")
        types = [c.component_type for c in result]
        self.assertIn("list", types)

    def test_collection_view(self):
        xml = "<hierarchy><UICollectionView /></hierarchy>"
        result = detect_ui_components(xml, platform="ios")
        types = [c.component_type for c in result]
        self.assertIn("collection", types)

    def test_search_bar(self):
        xml = "<hierarchy><UISearchBar /></hierarchy>"
        result = detect_ui_components(xml, platform="ios")
        types = [c.component_type for c in result]
        self.assertIn("search_bar", types)

    def test_segmented_control(self):
        xml = "<hierarchy><UISegmentedControl /></hierarchy>"
        result = detect_ui_components(xml, platform="ios")
        types = [c.component_type for c in result]
        self.assertIn("segmented_control", types)

    def test_ios_does_not_detect_android_tags(self):
        xml = (
            "<hierarchy>"
            "<com.google.android.material.bottomnavigation"
            ".BottomNavigationView />"
            "</hierarchy>"
        )
        result = detect_ui_components(xml, platform="ios")
        self.assertEqual(result, [])


class TestResourceIdPatternMatching(unittest.TestCase):
    """Tests for resource ID based detection."""

    def test_bottom_nav_resource_id(self):
        xml = (
            "<hierarchy>"
            '<android.widget.FrameLayout resource-id="com.app:id/bottom_nav_container" />'
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("bottom_navigation", types)
        # Resource ID match should have lower confidence
        nav_comp = next(c for c in result if c.component_type == "bottom_navigation")
        self.assertEqual(nav_comp.confidence, 0.7)

    def test_fab_resource_id(self):
        xml = (
            "<hierarchy>"
            '<android.widget.ImageButton resource-id="com.app:id/fab_create" />'
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("fab", types)

    def test_recycler_resource_id(self):
        xml = (
            "<hierarchy>"
            '<android.widget.FrameLayout resource-id="com.app:id/recycler_list" />'
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("list", types)

    def test_drawer_resource_id(self):
        xml = (
            "<hierarchy>"
            '<android.widget.FrameLayout resource-id="com.app:id/nav_drawer_layout" />'
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("navigation_drawer", types)


class TestClassAttributeMatching(unittest.TestCase):
    """Tests for class attribute based detection."""

    def test_class_attribute_match(self):
        xml = (
            "<hierarchy>"
            "<android.view.View class="
            '"com.google.android.material.floatingactionbutton'
            '.FloatingActionButton" />'
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        types = [c.component_type for c in result]
        self.assertIn("fab", types)


class TestDeduplication(unittest.TestCase):
    """Tests for component type deduplication."""

    def test_duplicate_list_components_deduped(self):
        xml = (
            "<hierarchy>"
            "<androidx.recyclerview.widget.RecyclerView />"
            "<android.widget.ListView />"
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        list_comps = [c for c in result if c.component_type == "list"]
        self.assertEqual(len(list_comps), 1)

    def test_tag_and_resource_id_deduped(self):
        xml = (
            "<hierarchy>"
            '<android.widget.SearchView resource-id="com.app:id/search_bar" />'
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        search_comps = [c for c in result if c.component_type == "search_bar"]
        # Tag match takes priority, only one component
        self.assertEqual(len(search_comps), 1)
        self.assertEqual(search_comps[0].confidence, 1.0)

    def test_multiple_different_types_detected(self):
        xml = (
            "<hierarchy>"
            "<com.google.android.material.bottomnavigation"
            ".BottomNavigationView />"
            "<com.google.android.material.floatingactionbutton"
            ".FloatingActionButton />"
            "<androidx.recyclerview.widget.RecyclerView />"
            "</hierarchy>"
        )
        result = detect_ui_components(xml)
        types = {c.component_type for c in result}
        self.assertIn("bottom_navigation", types)
        self.assertIn("fab", types)
        self.assertIn("list", types)
        self.assertEqual(len(result), 3)


class TestPlatformSpecific(unittest.TestCase):
    """Tests for platform-specific detection."""

    def test_default_platform_is_android(self):
        xml = "<hierarchy><android.widget.ListView /></hierarchy>"
        result = detect_ui_components(xml)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].component_type, "list")

    def test_android_tags_not_detected_on_ios(self):
        xml = "<hierarchy><android.widget.ListView /></hierarchy>"
        result = detect_ui_components(xml, platform="ios")
        self.assertEqual(result, [])

    def test_ios_tags_not_detected_on_android(self):
        xml = "<hierarchy><UITabBar /></hierarchy>"
        result = detect_ui_components(xml, platform="android")
        self.assertEqual(result, [])


class TestDetectedComponentDataclass(unittest.TestCase):
    """Tests for DetectedComponent dataclass defaults."""

    def test_defaults(self):
        comp = DetectedComponent(component_type="fab")
        self.assertEqual(comp.component_type, "fab")
        self.assertEqual(comp.tag, "")
        self.assertEqual(comp.confidence, 1.0)
        self.assertEqual(comp.resource_id, "")


if __name__ == "__main__":
    unittest.main()
