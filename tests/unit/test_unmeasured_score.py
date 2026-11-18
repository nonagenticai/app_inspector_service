"""A total inspection failure must never render as a perfect audit.

``overall_acc = sum(...) // len(...) if acc_scores else 100`` turned "we
measured NOTHING" into "100/100" — and ``export_service`` printed that into the
CUSTOMER-facing report next to "Screens Discovered: 0".

The same default sat at four more layers, each of which independently
manufactures a perfect score out of missing data:

* ``_score(total_checked <= 0) -> 100``
* ``analyze_accessibility`` returning a default ``AccessibilityReport`` on a
  blank or malformed page source
* ``ScreenInfo.accessibility_score`` / ``_build_screen_info``'s ``.get(..., 100)``
* ``ExportService``'s ``.get("accessibility_score", 100)``

DECISION: "not measured" is represented as ``None`` (absent), never as a
number. A score is a measurement; inventing one is the bug. ``None`` propagates
from the analyzer through the schema to the report, where it renders as
"Not measured".
"""

from __future__ import annotations

from lxml import etree

from src.api.routes._helpers import _build_screen_info
from src.services.accessibility_analyzer import _score, analyze_accessibility
from src.services.export_service import ExportService

# A page source with one clickable element that HAS a label: measurable, clean.
_GOOD_XML = (
    '<hierarchy><android.widget.Button clickable="true" content-desc="Log in" '
    'bounds="[0,0][200,200]" /></hierarchy>'
)


class TestAnalyzerReportsNotMeasured:
    def test_blank_source_scores_none_not_100(self):
        assert analyze_accessibility("").score is None

    def test_whitespace_source_scores_none_not_100(self):
        assert analyze_accessibility("   \n  ").score is None

    def test_malformed_xml_scores_none_not_100(self):
        assert analyze_accessibility("<hierarchy><unclosed>").score is None

    def test_no_checkable_elements_scores_none_not_100(self):
        """Parsed fine, but nothing in it was scoreable."""
        assert (
            analyze_accessibility("<hierarchy><android.widget.TextView /></hierarchy>").score
            is None
        )

    def test_zero_total_checked_scores_none_not_100(self):
        assert _score([], 0) is None

    def test_a_real_measurement_still_scores(self):
        """The guard must not swallow genuine 100s — a clean measured screen."""
        report = analyze_accessibility(_GOOD_XML)
        assert report.total_elements_checked == 1
        assert report.score == 100


class TestResultAggregation:
    def test_zero_screens_yields_no_overall_score(self):
        from src.api.routes.jobs import _overall_accessibility_score

        assert _overall_accessibility_score([]) is None

    def test_screens_without_measurements_yield_no_overall_score(self):
        from src.api.routes.jobs import _overall_accessibility_score

        assert _overall_accessibility_score([{"screen_id": "a"}, {"screen_id": "b"}]) is None

    def test_measured_screens_average_normally(self):
        from src.api.routes.jobs import _overall_accessibility_score

        assert (
            _overall_accessibility_score([{"accessibility_score": 80}, {"accessibility_score": 60}])
            == 70
        )

    def test_unmeasured_screens_do_not_inflate_the_average(self):
        """An unmeasured screen must be EXCLUDED, not counted as 100."""
        from src.api.routes.jobs import _overall_accessibility_score

        assert (
            _overall_accessibility_score([{"accessibility_score": 60}, {"screen_id": "unmeasured"}])
            == 60
        )


class TestScreenInfoDoesNotInventAScore:
    def test_missing_score_stays_absent(self):
        assert _build_screen_info({"screen_id": "s1"}).accessibility_score is None

    def test_present_score_is_preserved(self):
        assert (
            _build_screen_info({"screen_id": "s1", "accessibility_score": 42}).accessibility_score
            == 42
        )


class TestCustomerReportNeverPrintsAFabricatedScore:
    def test_html_zero_screens_does_not_print_100(self):
        html = ExportService().export_html(
            job_id="job-1",
            platform="android",
            screens=[],
            navigation_graph=[],
            overall_accessibility_score=None,
        )
        assert "Screens Discovered:</strong> 0" in html
        assert "100/100" not in html
        assert "Not measured" in html

    def test_html_default_overall_is_not_a_perfect_score(self):
        """The DEFAULT argument itself must not be 100 — callers omit it."""
        html = ExportService().export_html(
            job_id="job-1", platform="android", screens=[], navigation_graph=[]
        )
        assert "100/100" not in html

    def test_html_unmeasured_screen_card_does_not_print_100(self):
        html = ExportService().export_html(
            job_id="job-1",
            platform="android",
            screens=[{"screen_id": "s1"}],
            navigation_graph=[],
            overall_accessibility_score=None,
        )
        assert "100/100" not in html

    def test_html_measured_score_still_renders(self):
        html = ExportService().export_html(
            job_id="job-1",
            platform="android",
            screens=[{"screen_id": "s1", "accessibility_score": 73}],
            navigation_graph=[],
            overall_accessibility_score=73,
        )
        assert "73/100" in html

    def test_xml_unmeasured_screen_has_no_fabricated_score(self):
        xml = ExportService().export_xml_sitemap(
            job_id="job-1",
            platform="android",
            screens=[{"screen_id": "s1"}],
            navigation_graph=[],
        )
        root = etree.fromstring(xml.encode("utf-8"))
        screen = root.find(".//screen")
        assert screen is not None
        assert screen.get("accessibility-score") is None

    def test_xml_measured_screen_keeps_its_score(self):
        xml = ExportService().export_xml_sitemap(
            job_id="job-1",
            platform="android",
            screens=[{"screen_id": "s1", "accessibility_score": 55}],
            navigation_graph=[],
        )
        root = etree.fromstring(xml.encode("utf-8"))
        screen = root.find(".//screen")
        assert screen is not None
        assert screen.get("accessibility-score") == "55"
