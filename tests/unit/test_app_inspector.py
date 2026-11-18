"""Unit tests for app_inspector_service."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.services.screen_classifier import (
    ALL_SCREEN_TYPES,
    DETAIL,
    FORM,
    HOME,
    LIST,
    LOGIN,
    ONBOARDING,
    PROFILE,
    SEARCH,
    SETTINGS,
    TAB_BAR,
    UNKNOWN,
    classify_screen,
)
from src.services.xml_parser import ParsedElement, ParsedScreen, parse_xml_source

# ==========================================================================
# Screen Classifier tests
# ==========================================================================


class TestScreenClassifier(unittest.TestCase):
    def test_login_password_field(self):
        xml = '<hierarchy><android.widget.EditText password="true" text="Password" /></hierarchy>'  # pragma: allowlist secret
        result = classify_screen(xml)
        self.assertEqual(result, LOGIN)

    def test_login_text_pattern(self):
        result = classify_screen("", text_content="Please sign in to continue")
        self.assertEqual(result, LOGIN)

    def test_onboarding_pattern(self):
        result = classify_screen("", text_content="Welcome! Get started with the app")
        self.assertEqual(result, ONBOARDING)

    def test_settings_pattern(self):
        result = classify_screen("", text_content="Settings and preferences")
        self.assertEqual(result, SETTINGS)

    def test_search_pattern(self):
        result = classify_screen("", text_content="Search for items")
        self.assertEqual(result, SEARCH)

    def test_profile_pattern(self):
        result = classify_screen("", text_content="My Account - Edit Profile")
        self.assertEqual(result, PROFILE)

    def test_form_pattern(self):
        result = classify_screen("", text_content="Sign up and create account")
        # sign_up/create_account matches FORM
        self.assertEqual(result, FORM)

    def test_list_pattern(self):
        result = classify_screen("<hierarchy><recycler/></hierarchy>", text_content="Feed timeline")
        self.assertEqual(result, LIST)

    def test_home_activity(self):
        result = classify_screen("<hierarchy />", activity_name="com.app.MainActivity")
        self.assertEqual(result, HOME)

    def test_detail_activity(self):
        result = classify_screen("<hierarchy />", activity_name="com.app.DetailView")
        self.assertEqual(result, DETAIL)

    def test_tab_bar(self):
        result = classify_screen("<hierarchy><BottomNavigation /></hierarchy>")
        self.assertEqual(result, TAB_BAR)

    def test_unknown_fallback(self):
        result = classify_screen("<hierarchy><SomeWidget /></hierarchy>")
        self.assertEqual(result, UNKNOWN)

    def test_all_screen_types_defined(self):
        self.assertGreater(len(ALL_SCREEN_TYPES), 10)
        self.assertIn(LOGIN, ALL_SCREEN_TYPES)
        self.assertIn(UNKNOWN, ALL_SCREEN_TYPES)

    def test_accessibility_ids_used(self):
        result = classify_screen("", accessibility_ids=["login_button", "password_field"])
        self.assertEqual(result, LOGIN)


# ==========================================================================
# XML Parser tests
# ==========================================================================


class TestXmlParser(unittest.TestCase):
    def test_empty_input(self):
        result = parse_xml_source("")
        self.assertIsInstance(result, ParsedScreen)
        self.assertEqual(result.interactive_elements, [])
        self.assertEqual(result.word_count, 0)

    def test_invalid_xml(self):
        result = parse_xml_source("<not valid xml")
        self.assertIsInstance(result, ParsedScreen)
        self.assertEqual(result.interactive_elements, [])

    def test_button_detection(self):
        xml = '<hierarchy><android.widget.Button text="Submit" clickable="true" /></hierarchy>'
        result = parse_xml_source(xml)
        self.assertEqual(len(result.interactive_elements), 1)
        self.assertEqual(result.interactive_elements[0].text, "Submit")
        self.assertTrue(result.interactive_elements[0].clickable)

    def test_text_extraction(self):
        xml = '<hierarchy><android.widget.TextView text="Hello World" /></hierarchy>'
        result = parse_xml_source(xml)
        self.assertIn("Hello World", result.text_content)
        self.assertGreater(result.word_count, 0)

    def test_accessibility_id_extraction(self):
        xml = '<hierarchy><element accessibility-id="btn_login" /></hierarchy>'
        result = parse_xml_source(xml)
        self.assertIn("btn_login", result.accessibility_ids)

    def test_content_desc_extraction(self):
        xml = '<hierarchy><element content-desc="Navigate back" /></hierarchy>'
        result = parse_xml_source(xml)
        self.assertIn("Navigate back", result.accessibility_ids)

    def test_ios_button_detection(self):
        xml = '<hierarchy><XCUIElementTypeButton label="Tap me" /></hierarchy>'
        result = parse_xml_source(xml)
        self.assertEqual(len(result.interactive_elements), 1)
        self.assertEqual(result.interactive_elements[0].text, "Tap me")

    def test_heading_detection(self):
        xml = '<hierarchy><android.widget.TextView text="Welcome" bounds="[0,100][500,200]" /></hierarchy>'
        result = parse_xml_source(xml)
        self.assertIn("Welcome", result.headings)

    def test_non_heading_position(self):
        xml = '<hierarchy><android.widget.TextView text="Footer" bounds="[0,2000][500,2100]" /></hierarchy>'
        result = parse_xml_source(xml)
        self.assertNotIn("Footer", result.headings)


# ==========================================================================
# ParsedElement tests
# ==========================================================================


class TestParsedElement(unittest.TestCase):
    def test_defaults(self):
        elem = ParsedElement(tag="button")
        self.assertEqual(elem.text, "")
        self.assertFalse(elem.clickable)
        self.assertTrue(elem.enabled)

    def test_full_element(self):
        elem = ParsedElement(
            tag="android.widget.Button",
            text="Submit",
            content_desc="Submit form",
            resource_id="btn_submit",
            bounds="[0,0][100,50]",
            clickable=True,
            enabled=True,
        )
        self.assertEqual(elem.tag, "android.widget.Button")
        self.assertEqual(elem.text, "Submit")
        self.assertTrue(elem.clickable)


# ==========================================================================
# Device Farm tests
# ==========================================================================


class TestDeviceFarm(unittest.TestCase):
    def test_local_farm_creation(self):
        from src.services.device_farm import get_device_farm

        farm = get_device_farm("local")
        self.assertEqual(farm.name, "local")

    def test_unknown_farm_raises(self):
        from src.services.device_farm import get_device_farm

        with self.assertRaises(ValueError) as ctx:
            get_device_farm("nonexistent")
        self.assertIn("nonexistent", str(ctx.exception))

    def test_local_farm_upload(self):
        from src.services.device_farm import LocalFarm

        farm = LocalFarm()
        result = asyncio.run(farm.upload_app("/path/to/app.apk"))
        self.assertEqual(result, "/path/to/app.apk")

    def test_local_farm_appium_url(self):
        from src.services.device_farm import LocalFarm

        farm = LocalFarm(appium_url="http://custom:4723")
        url = asyncio.run(farm.get_appium_url())
        self.assertEqual(url, "http://custom:4723")

    def test_local_farm_capabilities(self):
        from src.services.device_farm import LocalFarm

        farm = LocalFarm()
        caps = asyncio.run(farm.get_capabilities())
        self.assertEqual(
            caps,
            {
                "platformName": "Android",
                "appium:automationName": "UiAutomator2",
                "appium:noReset": True,
            },
        )

    def test_device_spec_defaults(self):
        from src.services.device_farm import DeviceSpec

        spec = DeviceSpec()
        self.assertEqual(spec.name, "default")
        self.assertEqual(spec.platform, "android")
        self.assertFalse(spec.real_device)

    def test_farm_providers_registered(self):
        from src.services.device_farm import FARM_PROVIDERS

        self.assertIn("local", FARM_PROVIDERS)
        self.assertIn("docker_android", FARM_PROVIDERS)
        self.assertIn("browserstack", FARM_PROVIDERS)


# ==========================================================================
# Storage Service tests
# ==========================================================================


class TestStorageService(unittest.TestCase):
    @patch("src.services.storage_service.get_settings")
    def test_store_and_retrieve(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                storage=MagicMock(backend="local", local_path=tmpdir)
            )
            from src.services.storage_service import StorageService

            svc = StorageService()

            path = asyncio.run(svc.store_bytes("job1", b"screenshot", "screen.png"))
            self.assertIn("job1", path)

            data = asyncio.run(svc.retrieve(path))
            self.assertEqual(data, b"screenshot")

    @patch("src.services.storage_service.get_settings")
    def test_retrieve_missing(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                storage=MagicMock(backend="local", local_path=tmpdir)
            )
            from src.services.storage_service import StorageService

            svc = StorageService()
            result = asyncio.run(svc.retrieve("/nonexistent/path"))
            self.assertIsNone(result)

    @patch("src.services.storage_service.get_settings")
    def test_screenshots_dir(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                storage=MagicMock(backend="local", local_path=tmpdir)
            )
            from src.services.storage_service import StorageService

            svc = StorageService()
            ss_dir = svc.get_screenshots_dir("test-job")
            self.assertTrue(ss_dir.exists())
            self.assertEqual(ss_dir.name, "screenshots")

    @patch("src.services.storage_service.get_settings")
    def test_list_screenshots_empty(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                storage=MagicMock(backend="local", local_path=tmpdir)
            )
            from src.services.storage_service import StorageService

            svc = StorageService()
            result = asyncio.run(svc.list_screenshots("test-job"))
            self.assertEqual(result, [])


# ==========================================================================
# Inspector Service tests
# ==========================================================================


class TestInspectorService(unittest.TestCase):
    def test_detect_platform_android(self):
        from src.services.inspector_service import _detect_platform

        self.assertEqual(_detect_platform("app.apk"), "android")
        self.assertEqual(_detect_platform("/path/to/my-app.APK"), "android")

    def test_detect_platform_ios(self):
        from src.services.inspector_service import _detect_platform

        self.assertEqual(_detect_platform("app.ipa"), "ios")
        self.assertEqual(_detect_platform("/path/to/my.app"), "ios")

    def test_detect_platform_default_android(self):
        from src.services.inspector_service import _detect_platform

        self.assertEqual(_detect_platform("somefile"), "android")


# ==========================================================================
# Screen Navigator constants tests
# ==========================================================================


class TestScreenNavigator(unittest.TestCase):
    def test_xml_hash_ignores_dynamic(self):
        from src.services.screen_navigator import _xml_hash

        xml1 = '<hierarchy bounds="[0,0][1080,1920]" instance="0" />'
        xml2 = '<hierarchy bounds="[0,0][1080,1920]" instance="1" />'
        self.assertEqual(_xml_hash(xml1), _xml_hash(xml2))

    def test_xml_hash_different_content(self):
        from src.services.screen_navigator import _xml_hash

        xml1 = '<hierarchy><button text="Login" /></hierarchy>'
        xml2 = '<hierarchy><button text="Register" /></hierarchy>'
        self.assertNotEqual(_xml_hash(xml1), _xml_hash(xml2))

    def test_max_depth_constant(self):
        from src.services.screen_navigator import _MAX_DEPTH

        self.assertEqual(_MAX_DEPTH, 5)


if __name__ == "__main__":
    unittest.main()
