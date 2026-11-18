"""Unit tests for purpose_inferrer."""

from __future__ import annotations

import unittest

from src.services.purpose_inferrer import infer_purpose_heuristic


class TestEmptyInput(unittest.TestCase):
    """Tests for empty/missing screen data."""

    def test_no_screens_returns_unable(self):
        result = infer_purpose_heuristic([], [])
        self.assertIn("Unable to determine app purpose", result)
        self.assertIn("no screens discovered", result)


class TestSocialMediaDetection(unittest.TestCase):
    """Tests for social media domain detection."""

    def test_social_keywords_detected(self):
        screens = [
            {
                "screen_type": "feed",
                "text_content": "profile feed timeline post comment like follow share",
                "has_forms": False,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("social media", result)

    def test_social_keywords_case_insensitive(self):
        screens = [
            {
                "screen_type": "unknown",
                "text_content": "Profile Feed Timeline Post Comment",
                "has_forms": False,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("social media", result)


class TestECommerceDetection(unittest.TestCase):
    """Tests for e-commerce domain detection."""

    def test_ecommerce_keywords_detected(self):
        screens = [
            {
                "screen_type": "product_list",
                "text_content": "cart checkout product price buy shop order payment",
                "has_forms": True,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("e commerce", result)


class TestNavigationComplexity(unittest.TestCase):
    """Tests for navigation complexity descriptions."""

    def test_simple_navigation(self):
        screens = [
            {"screen_type": "home", "text_content": "Home", "has_forms": False},
            {"screen_type": "settings", "text_content": "Settings", "has_forms": False},
        ]
        edges = [
            {"from_screen": "s0", "to_screen": "s1", "action": "tap"},
        ]
        result = infer_purpose_heuristic(screens, edges)
        self.assertIn("simple, linear navigation", result)

    def test_moderate_navigation(self):
        screens = [
            {"screen_type": "home", "text_content": "Home", "has_forms": False},
            {"screen_type": "settings", "text_content": "Settings", "has_forms": False},
        ]
        edges = [
            {"from_screen": "s0", "to_screen": "s1", "action": "tap"},
            {"from_screen": "s1", "to_screen": "s0", "action": "tap"},
            {"from_screen": "s0", "to_screen": "s2", "action": "tap"},
        ]
        result = infer_purpose_heuristic(screens, edges)
        self.assertIn("moderate navigation depth", result)

    def test_complex_navigation(self):
        screens = [
            {"screen_type": "home", "text_content": "Home", "has_forms": False},
            {"screen_type": "detail", "text_content": "Detail", "has_forms": False},
        ]
        edges = [
            {"from_screen": "s0", "to_screen": "s1", "action": "tap"},
            {"from_screen": "s1", "to_screen": "s0", "action": "tap"},
            {"from_screen": "s0", "to_screen": "s2", "action": "tap"},
            {"from_screen": "s2", "to_screen": "s3", "action": "tap"},
            {"from_screen": "s3", "to_screen": "s0", "action": "tap"},
        ]
        result = infer_purpose_heuristic(screens, edges)
        self.assertIn("complex, interconnected navigation", result)


class TestFeatureDetection(unittest.TestCase):
    """Tests for feature detection in purpose description."""

    def test_login_feature_detected(self):
        screens = [
            {
                "screen_type": "login",
                "text_content": "username password sign in",
                "has_forms": True,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("user authentication", result)

    def test_search_feature_detected(self):
        screens = [
            {
                "screen_type": "search",
                "text_content": "search results",
                "has_forms": False,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("search functionality", result)

    def test_list_feature_detected(self):
        screens = [
            {
                "screen_type": "list",
                "text_content": "item 1 item 2 item 3",
                "has_forms": False,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("list-based content", result)

    def test_forms_feature_detected(self):
        screens = [
            {
                "screen_type": "form",
                "text_content": "enter your name email",
                "has_forms": True,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("data entry forms", result)

    def test_max_three_features(self):
        screens = [
            {
                "screen_type": "login",
                "text_content": "search results",
                "has_forms": True,
            },
            {
                "screen_type": "search",
                "text_content": "search again",
                "has_forms": False,
            },
            {
                "screen_type": "list",
                "text_content": "items list",
                "has_forms": False,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        # Should have at most 3 features listed
        feature_text = result.split("featuring ")[-1] if "featuring " in result else ""
        if feature_text:
            # Count commas + 1 to get feature count (rough check)
            features = feature_text.rstrip(".").split(", ")
            self.assertLessEqual(len(features), 3)


class TestFallbackDescriptions(unittest.TestCase):
    """Tests for fallback descriptions when no domain is matched."""

    def test_list_and_search_fallback(self):
        screens = [
            {"screen_type": "list", "text_content": "", "has_forms": False},
            {"screen_type": "search", "text_content": "", "has_forms": False},
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("content browsing", result)

    def test_forms_and_login_fallback(self):
        screens = [
            {"screen_type": "login", "text_content": "", "has_forms": True},
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("form-based application", result)
        self.assertIn("user accounts", result)

    def test_tab_bar_fallback(self):
        screens = [
            {"screen_type": "tab_bar", "text_content": "", "has_forms": False},
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("multi-section application", result)
        self.assertIn("tab-based navigation", result)

    def test_generic_fallback(self):
        screens = [
            {"screen_type": "unknown", "text_content": "", "has_forms": False},
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("general-purpose mobile application", result)


class TestDomainPriority(unittest.TestCase):
    """Tests for domain scoring priority."""

    def test_highest_scoring_domain_wins(self):
        # Finance keywords appear more often than social
        screens = [
            {
                "screen_type": "unknown",
                "text_content": (
                    "bank transfer balance transaction account pay wallet "
                    "bank bank transfer balance profile"
                ),
                "has_forms": False,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("finance", result)

    def test_multiple_screens_aggregated(self):
        screens = [
            {
                "screen_type": "unknown",
                "text_content": "cart product price",
                "has_forms": False,
            },
            {
                "screen_type": "unknown",
                "text_content": "checkout buy shop order",
                "has_forms": False,
            },
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIn("e commerce", result)


class TestOutputFormat(unittest.TestCase):
    """Tests for output formatting."""

    def test_ends_with_period(self):
        screens = [
            {"screen_type": "home", "text_content": "hello", "has_forms": False},
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertTrue(result.endswith("."))

    def test_returns_string(self):
        screens = [
            {"screen_type": "home", "text_content": "hello", "has_forms": False},
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIsInstance(result, str)

    def test_none_text_content_handled(self):
        screens = [
            {"screen_type": "home", "text_content": None, "has_forms": False},
        ]
        result = infer_purpose_heuristic(screens, [])
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)


if __name__ == "__main__":
    unittest.main()
