"""Tests for the export service (HTML and XML formats)."""

from __future__ import annotations

import pytest
from lxml import etree

from src.services.export_service import ExportService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _screen(
    screen_id: str = "screen_1",
    screen_type: str = "content",
    activity_name: str | None = None,
    text_content: str | None = None,
    interactive_elements: int = 3,
    accessibility_score: int = 100,
    accessibility_issues: list[dict] | None = None,
) -> dict:
    return {
        "screen_id": screen_id,
        "screen_type": screen_type,
        "activity_name": activity_name,
        "text_content": text_content,
        "interactive_elements": interactive_elements,
        "accessibility_score": accessibility_score,
        "accessibility_issues": accessibility_issues or [],
    }


def _nav(from_screen: str, to_screen: str, action: str = "tap") -> dict:
    return {"from_screen": from_screen, "to_screen": to_screen, "action": action}


@pytest.fixture
def export_service() -> ExportService:
    return ExportService()


# ---------------------------------------------------------------------------
# Tests: export_html
# ---------------------------------------------------------------------------


class TestExportHTML:
    def test_returns_valid_html_with_job_id(self, export_service: ExportService) -> None:
        html = export_service.export_html(
            job_id="job-123",
            platform="android",
            screens=[_screen()],
            navigation_graph=[],
        )
        assert "<!DOCTYPE html>" in html
        assert "job-123" in html

    def test_includes_platform(self, export_service: ExportService) -> None:
        html = export_service.export_html(
            job_id="job-1",
            platform="ios",
            screens=[],
            navigation_graph=[],
        )
        assert "ios" in html

    def test_includes_screens_count(self, export_service: ExportService) -> None:
        screens = [_screen("s1"), _screen("s2"), _screen("s3")]
        html = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=screens,
            navigation_graph=[],
        )
        assert "Screens Discovered:</strong> 3" in html

    def test_includes_accessibility_issues(self, export_service: ExportService) -> None:
        issues = [
            {"severity": "error", "rule": "missing-label", "message": "No label on button"},
            {"severity": "warning", "rule": "low-contrast", "message": "Contrast too low"},
        ]
        screens = [_screen(accessibility_issues=issues)]
        html = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=screens,
            navigation_graph=[],
        )
        assert "missing-label" in html
        assert "No label on button" in html
        assert "low-contrast" in html
        assert "[ERROR]" in html
        assert "[WARNING]" in html

    def test_handles_empty_screens_list(self, export_service: ExportService) -> None:
        html = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=[],
            navigation_graph=[],
        )
        assert "<!DOCTYPE html>" in html
        assert "Screens Discovered:</strong> 0" in html

    def test_includes_estimated_purpose(self, export_service: ExportService) -> None:
        html = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=[],
            navigation_graph=[],
            estimated_purpose="E-commerce shopping app",
        )
        assert "E-commerce shopping app" in html
        assert "Estimated Purpose" in html

    def test_omits_estimated_purpose_when_none(self, export_service: ExportService) -> None:
        html = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=[],
            navigation_graph=[],
            estimated_purpose=None,
        )
        assert "Estimated Purpose" not in html

    def test_includes_navigation_graph(self, export_service: ExportService) -> None:
        nav = [_nav("s1", "s2", "tap"), _nav("s2", "s3", "swipe")]
        html = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=[],
            navigation_graph=nav,
        )
        assert "s1" in html
        assert "s2" in html
        assert "s3" in html
        assert "(tap)" in html
        assert "(swipe)" in html

    def test_includes_overall_accessibility_score(self, export_service: ExportService) -> None:
        html = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=[],
            navigation_graph=[],
            overall_accessibility_score=85,
        )
        assert "85/100" in html

    def test_platform_none_shows_na(self, export_service: ExportService) -> None:
        html = export_service.export_html(
            job_id="job-1",
            platform=None,
            screens=[],
            navigation_graph=[],
        )
        assert "N/A" in html


# ---------------------------------------------------------------------------
# Tests: export_xml_sitemap
# ---------------------------------------------------------------------------


class TestExportXMLSitemap:
    def test_returns_valid_xml(self, export_service: ExportService) -> None:
        xml_str = export_service.export_xml_sitemap(
            job_id="job-123",
            platform="android",
            screens=[_screen()],
            navigation_graph=[],
        )
        # Should parse without errors
        root = etree.fromstring(xml_str.encode("utf-8"))
        assert root.tag == "app-structure"
        assert root.get("job_id") == "job-123"
        assert root.get("platform") == "android"

    def test_includes_all_screens(self, export_service: ExportService) -> None:
        screens = [
            _screen("s1", screen_type="login", activity_name="com.app.Login"),
            _screen("s2", screen_type="home", activity_name="com.app.Main"),
            _screen("s3", screen_type="settings"),
        ]
        xml_str = export_service.export_xml_sitemap(
            job_id="job-1",
            platform="android",
            screens=screens,
            navigation_graph=[],
        )
        root = etree.fromstring(xml_str.encode("utf-8"))
        screens_elem = root.find("screens")
        assert screens_elem is not None
        assert screens_elem.get("count") == "3"
        screen_elems = screens_elem.findall("screen")
        assert len(screen_elems) == 3
        ids = {s.get("id") for s in screen_elems}
        assert ids == {"s1", "s2", "s3"}

    def test_includes_navigation_edges(self, export_service: ExportService) -> None:
        nav = [
            _nav("s1", "s2", "tap"),
            _nav("s2", "s3", "swipe"),
        ]
        xml_str = export_service.export_xml_sitemap(
            job_id="job-1",
            platform="android",
            screens=[],
            navigation_graph=nav,
        )
        root = etree.fromstring(xml_str.encode("utf-8"))
        nav_elem = root.find("navigation")
        assert nav_elem is not None
        assert nav_elem.get("edges") == "2"
        edge_elems = nav_elem.findall("edge")
        assert len(edge_elems) == 2

    def test_handles_empty_data(self, export_service: ExportService) -> None:
        xml_str = export_service.export_xml_sitemap(
            job_id="job-empty",
            platform=None,
            screens=[],
            navigation_graph=[],
        )
        root = etree.fromstring(xml_str.encode("utf-8"))
        assert root.tag == "app-structure"
        assert root.get("job_id") == "job-empty"
        assert root.get("platform") is None
        screens_elem = root.find("screens")
        assert screens_elem is not None
        assert screens_elem.get("count") == "0"
        nav_elem = root.find("navigation")
        assert nav_elem is not None
        assert nav_elem.get("edges") == "0"

    def test_screen_attributes(self, export_service: ExportService) -> None:
        screens = [
            _screen(
                "s1",
                screen_type="login",
                activity_name="com.app.Login",
                accessibility_score=85,
                interactive_elements=5,
            )
        ]
        xml_str = export_service.export_xml_sitemap(
            job_id="job-1",
            platform="android",
            screens=screens,
            navigation_graph=[],
        )
        root = etree.fromstring(xml_str.encode("utf-8"))
        screen_elem = root.find("screens/screen")
        assert screen_elem is not None
        assert screen_elem.get("id") == "s1"
        assert screen_elem.get("type") == "login"
        assert screen_elem.get("activity") == "com.app.Login"
        assert screen_elem.get("accessibility-score") == "85"
        assert screen_elem.get("interactive-elements") == "5"

    def test_xml_declaration_present(self, export_service: ExportService) -> None:
        xml_str = export_service.export_xml_sitemap(
            job_id="job-1",
            platform="android",
            screens=[],
            navigation_graph=[],
        )
        assert xml_str.startswith("<?xml")
