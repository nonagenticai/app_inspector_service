"""Unit tests for accessibility_analyzer."""

from __future__ import annotations

import unittest

from src.services.accessibility_analyzer import (
    AccessibilityIssue,
    AccessibilityReport,
    analyze_accessibility,
)


class TestEmptyAndInvalidInput(unittest.TestCase):
    """Tests for empty/invalid XML input."""

    def test_empty_string_returns_empty_report(self):
        report = analyze_accessibility("")
        self.assertIsNone(report.score)  # nothing measured => no score
        self.assertEqual(report.issues, [])
        self.assertEqual(report.total_elements_checked, 0)

    def test_none_returns_empty_report(self):
        report = analyze_accessibility("")
        self.assertIsNone(report.score)  # nothing measured => no score
        self.assertEqual(report.issues, [])

    def test_whitespace_only_returns_empty_report(self):
        report = analyze_accessibility("   \n  ")
        self.assertIsNone(report.score)  # nothing measured => no score
        self.assertEqual(report.issues, [])

    def test_invalid_xml_returns_empty_report(self):
        report = analyze_accessibility("<not-valid<<xml")
        self.assertIsNone(report.score)  # unparseable => no score
        self.assertEqual(report.issues, [])

    def test_xml_with_no_interactive_elements(self):
        xml = '<hierarchy><android.widget.TextView text="Hello" /></hierarchy>'
        report = analyze_accessibility(xml)
        self.assertIsNone(report.score)  # no scoreable element => no score
        self.assertEqual(report.issues, [])
        self.assertEqual(report.total_elements_checked, 0)


class TestMissingContentDescription(unittest.TestCase):
    """Tests for Rule 1: missing content description on clickable elements."""

    def test_clickable_without_text_or_desc(self):
        xml = '<hierarchy><android.widget.ImageButton clickable="true" /></hierarchy>'
        report = analyze_accessibility(xml)
        self.assertTrue(len(report.issues) > 0)
        rules = [i.rule for i in report.issues]
        self.assertIn("missing_content_description", rules)
        issue = next(i for i in report.issues if i.rule == "missing_content_description")
        self.assertEqual(issue.severity, "error")

    def test_clickable_with_text_no_issue(self):
        xml = '<hierarchy><android.widget.Button clickable="true" text="Submit" /></hierarchy>'
        report = analyze_accessibility(xml)
        content_desc_issues = [i for i in report.issues if i.rule == "missing_content_description"]
        self.assertEqual(len(content_desc_issues), 0)

    def test_clickable_with_content_desc_no_issue(self):
        xml = (
            "<hierarchy>"
            '<android.widget.ImageButton clickable="true" '
            'content-desc="Close" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        content_desc_issues = [i for i in report.issues if i.rule == "missing_content_description"]
        self.assertEqual(len(content_desc_issues), 0)


class TestSmallTouchTargets(unittest.TestCase):
    """Tests for Rule 2: small touch targets."""

    def test_small_touch_target_android(self):
        xml = (
            "<hierarchy>"
            '<android.widget.Button clickable="true" text="OK" '
            'bounds="[0,0][30,30]" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml, platform="android")
        rules = [i.rule for i in report.issues]
        self.assertIn("small_touch_target", rules)
        issue = next(i for i in report.issues if i.rule == "small_touch_target")
        self.assertEqual(issue.severity, "warning")
        self.assertIn("30x30", issue.message)

    def test_adequate_touch_target_android(self):
        xml = (
            "<hierarchy>"
            '<android.widget.Button clickable="true" text="OK" '
            'bounds="[0,0][48,48]" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml, platform="android")
        touch_issues = [i for i in report.issues if i.rule == "small_touch_target"]
        self.assertEqual(len(touch_issues), 0)

    def test_small_touch_target_ios(self):
        xml = (
            "<hierarchy>"
            '<XCUIElementTypeButton clickable="true" label="OK" '
            'bounds="[0,0][40,40]" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml, platform="ios")
        rules = [i.rule for i in report.issues]
        self.assertIn("small_touch_target", rules)

    def test_adequate_touch_target_ios(self):
        xml = (
            "<hierarchy>"
            '<XCUIElementTypeButton clickable="true" label="OK" '
            'bounds="[0,0][44,44]" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml, platform="ios")
        touch_issues = [i for i in report.issues if i.rule == "small_touch_target"]
        self.assertEqual(len(touch_issues), 0)

    def test_no_bounds_no_touch_target_issue(self):
        xml = '<hierarchy><android.widget.Button clickable="true" text="OK" /></hierarchy>'
        report = analyze_accessibility(xml, platform="android")
        touch_issues = [i for i in report.issues if i.rule == "small_touch_target"]
        self.assertEqual(len(touch_issues), 0)


class TestMissingInputLabels(unittest.TestCase):
    """Tests for Rule 3: missing labels on input fields."""

    def test_edittext_without_label(self):
        xml = "<hierarchy><android.widget.EditText /></hierarchy>"
        report = analyze_accessibility(xml)
        rules = [i.rule for i in report.issues]
        self.assertIn("missing_input_label", rules)
        issue = next(i for i in report.issues if i.rule == "missing_input_label")
        self.assertEqual(issue.severity, "error")

    def test_edittext_with_text_no_issue(self):
        xml = '<hierarchy><android.widget.EditText text="Username" /></hierarchy>'
        report = analyze_accessibility(xml)
        input_issues = [i for i in report.issues if i.rule == "missing_input_label"]
        self.assertEqual(len(input_issues), 0)

    def test_edittext_with_content_desc_no_issue(self):
        xml = '<hierarchy><android.widget.EditText content-desc="Enter email" /></hierarchy>'
        report = analyze_accessibility(xml)
        input_issues = [i for i in report.issues if i.rule == "missing_input_label"]
        self.assertEqual(len(input_issues), 0)

    def test_ios_textfield_without_label(self):
        xml = "<hierarchy><XCUIElementTypeTextField /></hierarchy>"
        report = analyze_accessibility(xml, platform="ios")
        rules = [i.rule for i in report.issues]
        self.assertIn("missing_input_label", rules)

    def test_secure_textfield_without_label(self):
        xml = "<hierarchy><XCUIElementTypeSecureTextField /></hierarchy>"
        report = analyze_accessibility(xml, platform="ios")
        rules = [i.rule for i in report.issues]
        self.assertIn("missing_input_label", rules)


class TestDuplicateAccessibilityIds(unittest.TestCase):
    """Tests for Rule 4: duplicate accessibility IDs."""

    def test_duplicate_content_desc(self):
        xml = (
            "<hierarchy>"
            '<android.widget.Button clickable="true" '
            'content-desc="action_btn" text="Save" />'
            '<android.widget.Button clickable="true" '
            'content-desc="action_btn" text="Cancel" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        dup_issues = [i for i in report.issues if i.rule == "duplicate_accessibility_id"]
        self.assertTrue(len(dup_issues) >= 2)
        self.assertEqual(dup_issues[0].severity, "warning")

    def test_unique_ids_no_issue(self):
        xml = (
            "<hierarchy>"
            '<android.widget.Button clickable="true" '
            'content-desc="save_btn" text="Save" />'
            '<android.widget.Button clickable="true" '
            'content-desc="cancel_btn" text="Cancel" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        dup_issues = [i for i in report.issues if i.rule == "duplicate_accessibility_id"]
        self.assertEqual(len(dup_issues), 0)


class TestNestedClickableElements(unittest.TestCase):
    """Tests for Rule 5: nested clickable elements."""

    def test_nested_clickable(self):
        xml = (
            "<hierarchy>"
            '<android.widget.LinearLayout clickable="true">'
            '<android.widget.Button clickable="true" text="Go" />'
            "</android.widget.LinearLayout>"
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        nested_issues = [i for i in report.issues if i.rule == "nested_clickable"]
        self.assertTrue(len(nested_issues) >= 1)
        self.assertEqual(nested_issues[0].severity, "warning")

    def test_no_nested_clickable(self):
        xml = (
            "<hierarchy>"
            "<android.widget.LinearLayout>"
            '<android.widget.Button clickable="true" text="Go" />'
            "</android.widget.LinearLayout>"
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        nested_issues = [i for i in report.issues if i.rule == "nested_clickable"]
        self.assertEqual(len(nested_issues), 0)


class TestScoreCalculation(unittest.TestCase):
    """Tests for accessibility score calculation."""

    def test_no_issues_score_100(self):
        xml = (
            "<hierarchy>"
            '<android.widget.Button clickable="true" text="OK" '
            'bounds="[0,0][100,100]" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        self.assertEqual(report.score, 100)

    def test_one_error_penalizes_15(self):
        xml = (
            "<hierarchy>"
            '<android.widget.ImageButton clickable="true" '
            'bounds="[0,0][100,100]" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        error_count = sum(1 for i in report.issues if i.severity == "error")
        warning_count = sum(1 for i in report.issues if i.severity == "warning")
        expected = max(0, 100 - (error_count * 15) - (warning_count * 5))
        self.assertEqual(report.score, expected)
        # At least one error => score <= 85
        assert report.score is not None
        self.assertLessEqual(report.score, 85)

    def test_one_warning_penalizes_5(self):
        xml = (
            "<hierarchy>"
            '<android.widget.Button clickable="true" text="OK" '
            'bounds="[0,0][30,30]" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        # Small touch target is a warning
        warning_issues = [i for i in report.issues if i.severity == "warning"]
        self.assertTrue(len(warning_issues) >= 1)
        # Score should be 100 - 5 * warnings
        error_count = sum(1 for i in report.issues if i.severity == "error")
        warning_count = sum(1 for i in report.issues if i.severity == "warning")
        expected = max(0, 100 - (error_count * 15) - (warning_count * 5))
        self.assertEqual(report.score, expected)

    def test_score_never_below_zero(self):
        # Many errors should still result in score >= 0
        elems = "".join(
            '<android.widget.ImageButton clickable="true" bounds="[0,0][10,10]" />'
            for _ in range(20)
        )
        xml = f"<hierarchy>{elems}</hierarchy>"
        report = analyze_accessibility(xml)
        assert report.score is not None
        self.assertGreaterEqual(report.score, 0)

    def test_no_interactive_elements_is_not_measured(self):
        xml = '<hierarchy><android.widget.TextView text="Hello" /></hierarchy>'
        report = analyze_accessibility(xml)
        self.assertIsNone(report.score)  # no scoreable element => no score

    def test_elements_with_issues_count(self):
        xml = (
            "<hierarchy>"
            '<android.widget.ImageButton clickable="true" />'
            '<android.widget.Button clickable="true" text="OK" '
            'bounds="[0,0][100,100]" />'
            "</hierarchy>"
        )
        report = analyze_accessibility(xml)
        self.assertGreater(report.elements_with_issues, 0)
        self.assertEqual(report.total_elements_checked, 2)


class TestReportDataclass(unittest.TestCase):
    """Tests for report and issue dataclass defaults."""

    def test_default_report(self):
        report = AccessibilityReport()
        self.assertIsNone(report.score)  # default == not measured
        self.assertEqual(report.issues, [])
        self.assertEqual(report.total_elements_checked, 0)
        self.assertEqual(report.elements_with_issues, 0)

    def test_issue_defaults(self):
        issue = AccessibilityIssue(rule="test", severity="error")
        self.assertEqual(issue.element_tag, "")
        self.assertEqual(issue.element_text, "")
        self.assertEqual(issue.element_resource_id, "")
        self.assertEqual(issue.message, "")


if __name__ == "__main__":
    unittest.main()
