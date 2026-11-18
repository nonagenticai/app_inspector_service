"""Unit tests for MaestroClient with mocked subprocess calls."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.automation_client import AutomationConnectError, ScreenshotCaptureError
from src.services.maestro_client import MaestroClient


def _run(coro):
    """Helper to run a coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_process_mock(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a mock subprocess process."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode("utf-8"), stderr.encode("utf-8")))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


# ===========================================================================
# Hierarchy JSON-to-XML conversion tests
# ===========================================================================


class TestHierarchyToXml(unittest.TestCase):
    def setUp(self):
        self.client = MaestroClient(platform="android")

    def test_empty_dict(self):
        result = self.client._hierarchy_to_xml({})
        self.assertIn("<hierarchy>", result)
        self.assertIn("</hierarchy>", result)
        self.assertIn("<element", result)

    def test_simple_element(self):
        data = {
            "type": "android.widget.Button",
            "text": "Submit",
            "clickable": "true",
        }
        result = self.client._hierarchy_to_xml(data)
        self.assertIn("<hierarchy>", result)
        self.assertIn("android.widget.Button", result)
        self.assertIn('text="Submit"', result)
        self.assertIn('clickable="true"', result)

    def test_nested_children(self):
        data = {
            "type": "FrameLayout",
            "children": [
                {"type": "TextView", "text": "Hello"},
                {"type": "Button", "text": "OK", "clickable": "true"},
            ],
        }
        result = self.client._hierarchy_to_xml(data)
        self.assertIn("<FrameLayout", result)
        self.assertIn("</FrameLayout>", result)
        self.assertIn("<TextView", result)
        self.assertIn("<Button", result)
        self.assertIn('text="Hello"', result)
        self.assertIn('text="OK"', result)

    def test_list_input(self):
        data = [
            {"type": "Button", "text": "A"},
            {"type": "Button", "text": "B"},
        ]
        result = self.client._hierarchy_to_xml(data)
        self.assertIn('text="A"', result)
        self.assertIn('text="B"', result)

    def test_html_escaping(self):
        data = {"type": "TextView", "text": 'Hello & "World" <test>'}
        result = self.client._hierarchy_to_xml(data)
        self.assertIn("&amp;", result)
        self.assertIn("&quot;", result)
        self.assertIn("&lt;", result)

    def test_class_name_fallback(self):
        data = {"className": "android.view.View", "text": "Content"}
        result = self.client._hierarchy_to_xml(data)
        self.assertIn("android.view.View", result)

    def test_element_tag_default(self):
        data = {"text": "No type"}
        result = self.client._hierarchy_to_xml(data)
        self.assertIn("<element", result)

    def test_resource_id_and_content_desc(self):
        data = {
            "type": "Button",
            "resource-id": "com.app:id/btn",
            "content-desc": "Navigate back",
        }
        result = self.client._hierarchy_to_xml(data)
        self.assertIn('resource-id="com.app:id/btn"', result)
        self.assertIn('content-desc="Navigate back"', result)

    def test_bounds_attribute(self):
        data = {"type": "View", "bounds": "[0,100][200,300]"}
        result = self.client._hierarchy_to_xml(data)
        self.assertIn('bounds="[0,100][200,300]"', result)

    def test_non_dict_non_list_ignored(self):
        """Non-dict/list nodes should be silently ignored."""
        data = [42, "string", None, {"type": "Button", "text": "OK"}]
        result = self.client._hierarchy_to_xml(data)
        self.assertIn('text="OK"', result)
        # Primitives should not produce elements
        self.assertNotIn("42", result)


# ===========================================================================
# Connect / Disconnect tests
# ===========================================================================


class TestConnectDisconnect(unittest.TestCase):
    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_connect_calls_install_and_launch(self, mock_exec):
        mock_exec.return_value = _make_process_mock()
        client = MaestroClient(platform="android")
        _run(client.connect("/path/to/app.apk"))

        # Should have been called twice: install + launch
        self.assertEqual(mock_exec.call_count, 2)
        first_call_args = mock_exec.call_args_list[0][0]
        second_call_args = mock_exec.call_args_list[1][0]
        self.assertEqual(first_call_args, ("maestro", "install", "/path/to/app.apk"))
        self.assertEqual(second_call_args, ("maestro", "launch", "/path/to/app.apk"))

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_connect_raises_when_install_fails(self, mock_exec):
        """A failed install must NOT return cleanly.

        ``_run_maestro`` returns the rc rather than raising. When ``connect``
        discarded it, "no device attached" produced a job that ran to
        ``completed`` over the synthetic ``<hierarchy />`` that ``get_page_source``
        substitutes on the same rc — one empty screen, billed, for an app that
        was never installed.
        """
        mock_exec.return_value = _make_process_mock(stderr="no devices found", returncode=1)
        client = MaestroClient(platform="android")

        with self.assertRaises(AutomationConnectError) as ctx:
            _run(client.connect("/path/to/app.apk"))

        self.assertIn("install", str(ctx.exception))
        # It must not have gone on to 'launch' an app it never installed.
        self.assertEqual(mock_exec.call_count, 1)

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_connect_raises_when_launch_fails(self, mock_exec):
        mock_exec.side_effect = [
            _make_process_mock(returncode=0),
            _make_process_mock(stderr="activity not found", returncode=1),
        ]
        client = MaestroClient(platform="android")

        with self.assertRaises(AutomationConnectError) as ctx:
            _run(client.connect("/path/to/app.apk"))

        self.assertIn("launch", str(ctx.exception))

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_disconnect_is_noop(self, mock_exec):
        client = MaestroClient()
        _run(client.disconnect())
        mock_exec.assert_not_called()


# ===========================================================================
# get_page_source tests
# ===========================================================================


class TestGetPageSource(unittest.TestCase):
    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_get_page_source_success(self, mock_exec):
        hierarchy_json = json.dumps(
            {
                "type": "FrameLayout",
                "children": [
                    {"type": "TextView", "text": "Welcome"},
                ],
            }
        )
        mock_exec.return_value = _make_process_mock(stdout=hierarchy_json)
        client = MaestroClient()

        result = _run(client.get_page_source())
        self.assertIn("<hierarchy>", result)
        self.assertIn("<FrameLayout", result)
        self.assertIn('text="Welcome"', result)
        self.assertIn("</hierarchy>", result)

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_get_page_source_command_failure(self, mock_exec):
        mock_exec.return_value = _make_process_mock(returncode=1, stderr="error")
        client = MaestroClient()

        result = _run(client.get_page_source())
        self.assertEqual(result, "<hierarchy />")

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_get_page_source_invalid_json(self, mock_exec):
        mock_exec.return_value = _make_process_mock(stdout="not json at all")
        client = MaestroClient()

        result = _run(client.get_page_source())
        self.assertEqual(result, "<hierarchy />")


# ===========================================================================
# find_clickable_elements tests
# ===========================================================================


class TestFindClickableElements(unittest.TestCase):
    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_find_clickable_by_attribute(self, mock_exec):
        hierarchy = json.dumps(
            {
                "type": "FrameLayout",
                "children": [
                    {"type": "View", "text": "Label", "clickable": "false"},
                    {
                        "type": "View",
                        "text": "Click Me",
                        "clickable": "true",
                        "bounds": "[10,20][110,70]",
                    },
                ],
            }
        )
        mock_exec.return_value = _make_process_mock(stdout=hierarchy)
        client = MaestroClient()

        elements = _run(client.find_clickable_elements())
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["text"], "Click Me")
        self.assertEqual(elements[0]["location"], {"x": 10, "y": 20})
        self.assertEqual(elements[0]["size"], {"width": 100, "height": 50})

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_find_clickable_by_interactive_type(self, mock_exec):
        hierarchy = json.dumps(
            {
                "type": "FrameLayout",
                "children": [
                    {"type": "android.widget.Button", "text": "Submit"},
                    {"type": "android.widget.EditText", "text": ""},
                ],
            }
        )
        mock_exec.return_value = _make_process_mock(stdout=hierarchy)
        client = MaestroClient()

        elements = _run(client.find_clickable_elements())
        self.assertEqual(len(elements), 2)
        tags = {e["tag"] for e in elements}
        self.assertIn("android.widget.Button", tags)
        self.assertIn("android.widget.EditText", tags)

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_find_clickable_command_failure(self, mock_exec):
        mock_exec.return_value = _make_process_mock(returncode=1)
        client = MaestroClient()

        elements = _run(client.find_clickable_elements())
        self.assertEqual(elements, [])

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_find_clickable_no_bounds(self, mock_exec):
        hierarchy = json.dumps(
            {
                "type": "android.widget.Button",
                "text": "No Bounds",
            }
        )
        mock_exec.return_value = _make_process_mock(stdout=hierarchy)
        client = MaestroClient()

        elements = _run(client.find_clickable_elements())
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["location"], {"x": 0, "y": 0})
        self.assertEqual(elements[0]["size"], {"width": 0, "height": 0})


# ===========================================================================
# Tap and Back tests
# ===========================================================================


class TestTapAndBack(unittest.TestCase):
    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_tap(self, mock_exec):
        mock_exec.return_value = _make_process_mock()
        client = MaestroClient()

        _run(client.tap(100, 200))
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        self.assertEqual(call_args, ("maestro", "tap", "100", "200"))

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_back(self, mock_exec):
        mock_exec.return_value = _make_process_mock()
        client = MaestroClient()

        _run(client.back())
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        self.assertEqual(call_args, ("maestro", "back"))


# ===========================================================================
# Screenshot tests
# ===========================================================================


class TestScreenshot(unittest.TestCase):
    """A returned path is a positive assertion that a readable file exists.

    ``_run_maestro`` does not raise on rc != 0, so every one of these asserts
    the OUTCOME (no path handed back) rather than "no exception escaped".
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "shot.png")

    def _exec_that_writes(self, returncode: int = 0, write: bool = True):
        """Simulate maestro: optionally write the PNG, then exit with rc."""

        def _factory(*_args, **_kwargs):
            if write:
                Path(self.path).write_bytes(b"\x89PNG\r\n\x1a\n")
            return _make_process_mock(returncode=returncode)

        return _factory

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_returns_path_when_file_lands(self, mock_exec):
        mock_exec.side_effect = self._exec_that_writes()
        client = MaestroClient()

        result = _run(client.screenshot(self.path))

        self.assertEqual(result, self.path)
        self.assertTrue(Path(result).is_file())
        call_args = mock_exec.call_args[0]
        self.assertEqual(call_args, ("maestro", "screenshot", self.path))

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_nonzero_returncode_yields_no_path(self, mock_exec):
        mock_exec.side_effect = self._exec_that_writes(returncode=1, write=False)
        client = MaestroClient()

        with self.assertRaises(ScreenshotCaptureError) as ctx:
            _run(client.screenshot(self.path))

        self.assertIn("rc=1", str(ctx.exception))
        self.assertFalse(Path(self.path).exists())

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_rc_zero_but_no_file_yields_no_path(self, mock_exec):
        """maestro can exit 0 having written nothing -- rc alone is not proof."""
        mock_exec.side_effect = self._exec_that_writes(returncode=0, write=False)
        client = MaestroClient()

        with self.assertRaises(ScreenshotCaptureError):
            _run(client.screenshot(self.path))

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_empty_file_yields_no_path(self, mock_exec):
        def _factory(*_args, **_kwargs):
            Path(self.path).write_bytes(b"")
            return _make_process_mock(returncode=0)

        mock_exec.side_effect = _factory
        client = MaestroClient()

        with self.assertRaises(ScreenshotCaptureError):
            _run(client.screenshot(self.path))


# ===========================================================================
# get_current_activity tests
# ===========================================================================


class TestGetCurrentActivity(unittest.TestCase):
    def test_returns_empty_string(self):
        client = MaestroClient()
        result = _run(client.get_current_activity())
        self.assertEqual(result, "")


# ===========================================================================
# Recording tests
# ===========================================================================


class TestRecording(unittest.TestCase):
    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_start_recording(self, mock_exec):
        mock_exec.return_value = _make_process_mock()
        client = MaestroClient()
        _run(client.start_recording())
        call_args = mock_exec.call_args[0]
        self.assertEqual(call_args, ("maestro", "record", "start"))

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_stop_recording_returns_empty_bytes(self, mock_exec):
        mock_exec.return_value = _make_process_mock()
        client = MaestroClient()
        result = _run(client.stop_recording())
        self.assertEqual(result, b"")


# ===========================================================================
# Subprocess timeout handling tests
# ===========================================================================


class TestSubprocessTimeout(unittest.TestCase):
    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_timeout_raises(self, mock_exec):
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        mock_exec.return_value = proc

        client = MaestroClient()
        with self.assertRaises(TimeoutError) as ctx:
            _run(client._run_maestro("hierarchy", timeout=1))
        self.assertIn("timed out", str(ctx.exception))
        proc.kill.assert_called_once()

    @patch("src.services.maestro_client.asyncio.create_subprocess_exec")
    def test_run_maestro_returns_output(self, mock_exec):
        mock_exec.return_value = _make_process_mock(
            stdout="output data", stderr="warn", returncode=0
        )
        client = MaestroClient()
        stdout, stderr, rc = _run(client._run_maestro("test"))
        self.assertEqual(stdout, "output data")
        self.assertEqual(stderr, "warn")
        self.assertEqual(rc, 0)


# ===========================================================================
# Parse bounds helper tests
# ===========================================================================


class TestParseBounds(unittest.TestCase):
    def test_valid_bounds(self):
        loc, size = MaestroClient._parse_bounds("[10,20][110,70]")
        self.assertEqual(loc, {"x": 10, "y": 20})
        self.assertEqual(size, {"width": 100, "height": 50})

    def test_empty_bounds(self):
        loc, size = MaestroClient._parse_bounds("")
        self.assertEqual(loc, {"x": 0, "y": 0})
        self.assertEqual(size, {"width": 0, "height": 0})

    def test_invalid_bounds(self):
        loc, size = MaestroClient._parse_bounds("invalid")
        self.assertEqual(loc, {"x": 0, "y": 0})
        self.assertEqual(size, {"width": 0, "height": 0})

    def test_zero_bounds(self):
        loc, size = MaestroClient._parse_bounds("[0,0][0,0]")
        self.assertEqual(loc, {"x": 0, "y": 0})
        self.assertEqual(size, {"width": 0, "height": 0})


if __name__ == "__main__":
    unittest.main()
