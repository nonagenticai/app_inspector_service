"""Unit tests for AppiumClient wrapper."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.appium_client import AppiumClient
from src.services.automation_client import ScreenshotCaptureError
from src.utils.logging_config import clamp_credential_leaking_loggers


class TestAppiumClientInit:
    def test_default_init(self):
        client = AppiumClient()
        assert client._appium_url == "http://localhost:4723"
        assert client._platform == "android"
        assert client._capabilities == {}
        assert client._driver is None

    def test_custom_init(self):
        client = AppiumClient(
            appium_url="http://remote:4723",
            capabilities={"key": "val"},
            platform="ios",
        )
        assert client._appium_url == "http://remote:4723"
        assert client._platform == "ios"
        assert client._capabilities == {"key": "val"}


class TestSessionId:
    def test_session_id_none_when_no_driver(self):
        client = AppiumClient()
        assert client.session_id is None

    def test_session_id_from_driver(self):
        client = AppiumClient()
        client._driver = MagicMock()
        client._driver.session_id = "abc-123"
        assert client.session_id == "abc-123"


class TestDisconnect:
    async def test_disconnect_quits_driver(self):
        client = AppiumClient()
        mock_driver = MagicMock()
        client._driver = mock_driver

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock()
            mock_loop.return_value = loop

            await client.disconnect()

        assert client._driver is None
        loop.run_in_executor.assert_awaited_once()

    async def test_disconnect_noop_when_no_driver(self):
        client = AppiumClient()
        client._driver = None
        await client.disconnect()


class TestGetPageSource:
    async def test_raises_when_not_connected(self):
        client = AppiumClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.get_page_source()

    async def test_returns_page_source(self):
        client = AppiumClient()
        mock_driver = MagicMock()
        mock_driver.page_source = "<hierarchy />"
        client._driver = mock_driver

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(return_value="<hierarchy />")
            mock_loop.return_value = loop

            result = await client.get_page_source()

        assert result == "<hierarchy />"


class TestScreenshot:
    async def test_raises_when_not_connected(self):
        client = AppiumClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.screenshot("/tmp/test.png")

    @staticmethod
    def _inline_loop():
        """A loop whose run_in_executor actually runs the callable inline.

        The previous fake returned ``None`` for every executor call, which made
        a failed ``save_screenshot`` indistinguishable from a successful one.
        """
        loop = MagicMock()

        async def _run(_executor, fn):
            return fn()

        loop.run_in_executor = AsyncMock(side_effect=_run)
        return loop

    async def test_returns_path_when_file_lands(self, tmp_path):
        target = tmp_path / "shot.png"
        client = AppiumClient()
        driver = MagicMock()
        driver.save_screenshot = MagicMock(
            side_effect=lambda p: (Path(p).write_bytes(b"\x89PNG\r\n\x1a\n"), True)[1]
        )
        client._driver = driver

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = self._inline_loop()
            result = await client.screenshot(str(target))

        assert result == str(target)
        assert target.is_file()

    async def test_save_screenshot_false_yields_no_path(self, tmp_path):
        """``save_screenshot`` returns False on a write failure; it does not raise."""
        target = tmp_path / "shot.png"
        client = AppiumClient()
        driver = MagicMock()
        driver.save_screenshot = MagicMock(return_value=False)
        client._driver = driver

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = self._inline_loop()
            with pytest.raises(ScreenshotCaptureError, match="returned False"):
                await client.screenshot(str(target))

        assert not target.exists()

    async def test_true_but_no_file_yields_no_path(self, tmp_path):
        target = tmp_path / "shot.png"
        client = AppiumClient()
        driver = MagicMock()
        driver.save_screenshot = MagicMock(return_value=True)
        client._driver = driver

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = self._inline_loop()
            with pytest.raises(ScreenshotCaptureError, match="wrote no file"):
                await client.screenshot(str(target))


class TestFindClickableElements:
    async def test_returns_empty_when_no_driver(self):
        client = AppiumClient()
        result = await client.find_clickable_elements()
        assert result == []

    async def test_finds_elements(self):
        client = AppiumClient()
        mock_driver = MagicMock()
        client._driver = mock_driver

        mock_elem = MagicMock()
        mock_elem.get_attribute = MagicMock(
            side_effect=lambda attr: {
                "text": "Click me",
                "label": "",
                "content-desc": "desc",
                "resource-id": "btn_1",
            }.get(attr, "")
        )
        mock_elem.tag_name = "android.widget.Button"
        mock_elem.location = {"x": 10, "y": 20}
        mock_elem.size = {"width": 100, "height": 50}

        mock_driver.find_elements = MagicMock(return_value=[mock_elem])

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()

            async def run_executor(executor, func):
                return func()

            loop.run_in_executor = AsyncMock(side_effect=run_executor)
            mock_loop.return_value = loop

            result = await client.find_clickable_elements()

        assert len(result) == 1
        assert result[0]["text"] == "Click me"


class TestTap:
    async def test_raises_when_not_connected(self):
        client = AppiumClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.tap(100, 200)

    async def test_tap_calls_driver(self):
        client = AppiumClient()
        client._driver = MagicMock()

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock()
            mock_loop.return_value = loop

            await client.tap(100, 200)

        loop.run_in_executor.assert_awaited_once()


class TestBack:
    async def test_raises_when_not_connected(self):
        client = AppiumClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.back()


class TestGetCurrentActivity:
    async def test_returns_empty_when_no_driver(self):
        client = AppiumClient()
        result = await client.get_current_activity()
        assert result == ""

    async def test_android_returns_activity(self):
        client = AppiumClient(platform="android")
        mock_driver = MagicMock()
        mock_driver.current_activity = "com.app.MainActivity"
        client._driver = mock_driver

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(return_value="com.app.MainActivity")
            mock_loop.return_value = loop

            result = await client.get_current_activity()

        assert result == "com.app.MainActivity"

    async def test_ios_returns_empty(self):
        client = AppiumClient(platform="ios")
        client._driver = MagicMock()

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(return_value="")
            mock_loop.return_value = loop

            result = await client.get_current_activity()

        assert result == ""


class TestRecording:
    async def test_start_recording_noop_no_driver(self):
        client = AppiumClient()
        await client.start_recording()

    async def test_stop_recording_returns_empty_no_driver(self):
        client = AppiumClient()
        result = await client.stop_recording()
        assert result == b""

    async def test_stop_recording_returns_decoded_bytes(self):
        client = AppiumClient()
        client._driver = MagicMock()

        video_data = b"fake video content"
        b64_data = base64.b64encode(video_data).decode("utf-8")

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(return_value=b64_data)
            mock_loop.return_value = loop

            result = await client.stop_recording()

        assert result == video_data


# ==========================================================================
# connect() tests (lines 29-49)
# ==========================================================================


class TestConnect:
    async def test_connect_android(self):
        """Test connect() for Android platform."""
        client = AppiumClient(
            appium_url="http://localhost:4723",
            capabilities={"appium:noReset": True},
            platform="android",
        )

        mock_driver = MagicMock()
        mock_driver.session_id = "sess-1"

        mock_options_cls = MagicMock()
        mock_options_instance = MagicMock()
        mock_options_cls.return_value = mock_options_instance

        mock_ios_options_cls = MagicMock()

        with (
            patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop_fn,
            patch.dict(
                "sys.modules",
                {
                    "appium": MagicMock(),
                    "appium.webdriver": MagicMock(),
                    "appium.options": MagicMock(),
                    "appium.options.android": MagicMock(UiAutomator2Options=mock_options_cls),
                    "appium.options.ios": MagicMock(XCUITestOptions=mock_ios_options_cls),
                },
            ),
        ):
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(return_value=mock_driver)
            mock_loop_fn.return_value = loop

            await client.connect("/tmp/app.apk")

        assert client._driver is mock_driver
        mock_options_instance.set_capability.assert_called_once_with("appium:noReset", True)

    async def test_connect_ios(self):
        """Test connect() for iOS platform."""
        client = AppiumClient(
            appium_url="http://localhost:4723",
            capabilities={},
            platform="ios",
        )

        mock_driver = MagicMock()
        mock_driver.session_id = "sess-2"

        mock_android_cls = MagicMock()
        mock_ios_cls = MagicMock()
        mock_ios_instance = MagicMock()
        mock_ios_cls.return_value = mock_ios_instance

        with (
            patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop_fn,
            patch.dict(
                "sys.modules",
                {
                    "appium": MagicMock(),
                    "appium.webdriver": MagicMock(),
                    "appium.options": MagicMock(),
                    "appium.options.android": MagicMock(UiAutomator2Options=mock_android_cls),
                    "appium.options.ios": MagicMock(XCUITestOptions=mock_ios_cls),
                },
            ),
        ):
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(return_value=mock_driver)
            mock_loop_fn.return_value = loop

            await client.connect("/tmp/app.ipa")

        assert client._driver is mock_driver
        # iOS options should be used
        mock_ios_cls.assert_called_once()
        mock_android_cls.assert_not_called()


# ==========================================================================
# find_clickable_elements iOS xpath (line 88)
# ==========================================================================


class TestFindClickableElementsIOS:
    async def test_finds_ios_elements(self):
        """Test find_clickable_elements uses iOS xpath."""
        client = AppiumClient(platform="ios")
        mock_driver = MagicMock()
        client._driver = mock_driver

        mock_elem = MagicMock()
        mock_elem.get_attribute = MagicMock(
            side_effect=lambda attr: {
                "text": "",
                "label": "Tap me",
                "content-desc": "",
                "resource-id": "",
            }.get(attr, "")
        )
        mock_elem.tag_name = "XCUIElementTypeButton"
        mock_elem.location = {"x": 10, "y": 20}
        mock_elem.size = {"width": 100, "height": 50}

        mock_driver.find_elements = MagicMock(return_value=[mock_elem])

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()

            async def run_executor(executor, func):
                return func()

            loop.run_in_executor = AsyncMock(side_effect=run_executor)
            mock_loop.return_value = loop

            result = await client.find_clickable_elements()

        assert len(result) == 1
        # Should use iOS xpath
        mock_driver.find_elements.assert_called_once_with("xpath", "//*[@accessible='true']")


# ==========================================================================
# find_clickable_elements element exception (lines 102-103)
# ==========================================================================


class TestFindClickableElementsError:
    async def test_element_attribute_error_logged(self):
        """Test that element attribute error is caught gracefully."""
        client = AppiumClient(platform="android")
        mock_driver = MagicMock()
        client._driver = mock_driver

        bad_elem = MagicMock()
        bad_elem.get_attribute = MagicMock(side_effect=OSError("Stale element"))

        good_elem = MagicMock()
        good_elem.get_attribute = MagicMock(
            side_effect=lambda attr: {
                "text": "OK",
                "label": "",
                "content-desc": "",
                "resource-id": "btn",
            }.get(attr, "")
        )
        good_elem.tag_name = "Button"
        good_elem.location = {"x": 0, "y": 0}
        good_elem.size = {"width": 50, "height": 50}

        mock_driver.find_elements = MagicMock(return_value=[bad_elem, good_elem])

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()

            async def run_executor(executor, func):
                return func()

            loop.run_in_executor = AsyncMock(side_effect=run_executor)
            mock_loop.return_value = loop

            result = await client.find_clickable_elements()

        # Bad element skipped, good element returned
        assert len(result) == 1
        assert result[0]["text"] == "OK"


# ==========================================================================
# back() with driver (lines 123-125)
# ==========================================================================


class TestBackWithDriver:
    async def test_back_calls_driver(self):
        client = AppiumClient()
        client._driver = MagicMock()

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock()
            mock_loop.return_value = loop

            await client.back()

        loop.run_in_executor.assert_awaited_once()


# ==========================================================================
# get_current_activity exception (lines 137-138)
# ==========================================================================


class TestGetCurrentActivityException:
    async def test_exception_returns_empty(self):
        """Test that exceptions in get_current_activity return empty string."""
        client = AppiumClient(platform="android")
        client._driver = MagicMock()

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(side_effect=OSError("WebDriver error"))
            mock_loop.return_value = loop

            result = await client.get_current_activity()

        assert result == ""


# ==========================================================================
# start_recording with driver (lines 144-146)
# ==========================================================================


class TestStartRecordingWithDriver:
    async def test_start_recording_calls_driver(self):
        client = AppiumClient()
        client._driver = MagicMock()

        with patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop:
            loop = MagicMock()
            loop.run_in_executor = AsyncMock()
            mock_loop.return_value = loop

            await client.start_recording()

        loop.run_in_executor.assert_awaited_once()


class TestConnectDoesNotLogCredentials:
    """Regression: BrowserStack hub URLs embed `user:access_key@` and the
    connect() log line used to emit them verbatim into pod logs.

    We assert on ``record.getMessage()`` (the *formatted* line) rather than on
    ``record.args``. The log call uses ``%s`` lazy formatting, so the raw
    ``self._appium_url`` argument would sit on ``record.args`` untouched even
    for a correctly-redacting implementation that passes the masked string —
    asserting on args would therefore be unfixable-by-design. What actually
    reaches a log sink is the formatted message, so that is what must be
    clean. ``caplog.text`` is checked too as a belt-and-braces backstop.
    """

    async def test_connect_redacts_access_key_from_logs(self, caplog):
        access_key = "zzTOPSECRETkey42"  # pragma: allowlist secret
        client = AppiumClient(
            appium_url=(f"https://someuser:{access_key}@hub-cloud.browserstack.com/wd/hub"),
            capabilities={},
            platform="android",
        )

        mock_driver = MagicMock()
        mock_driver.session_id = "sess-redact"

        with (
            caplog.at_level(logging.INFO, logger="src.services.appium_client"),
            patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop_fn,
            patch.dict(
                "sys.modules",
                {
                    "appium": MagicMock(),
                    "appium.webdriver": MagicMock(),
                    "appium.options": MagicMock(),
                    "appium.options.android": MagicMock(UiAutomator2Options=MagicMock()),
                    "appium.options.ios": MagicMock(XCUITestOptions=MagicMock()),
                },
            ),
        ):
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(return_value=mock_driver)
            mock_loop_fn.return_value = loop

            await client.connect("/tmp/app.apk")

        assert caplog.records, "expected the connect() log line to be captured"
        for record in caplog.records:
            assert access_key not in record.getMessage()
            assert "someuser" not in record.getMessage()
        assert access_key not in caplog.text
        # The redacted host must still be there — redaction, not suppression.
        assert "hub-cloud.browserstack.com" in caplog.text
        # ...and the client keeps the working, credentialed URL.
        assert access_key in client._appium_url


class TestSeleniumDebugLoggerIsClamped:
    """Regression: ``LOG_LEVEL=DEBUG`` must not undo the connect() redaction.

    selenium re-logs the very URL we redact, one level down, at DEBUG:
    ``remote_connection.py`` emits ``"%s %s %s" % (method, url, body)`` where
    ``url`` still carries ``user:access_key@``. Production was safe only because
    ``LOG_LEVEL`` happens to be INFO/WARNING -- and the operator most likely to
    flip it to DEBUG is the one debugging a device-farm session.
    ``clamp_credential_leaking_loggers`` pins that logger at or above INFO.

    HOW THIS TEST AVOIDS PASSING VACUOUSLY -- ``caplog`` moves handler and
    logger levels around, so "no record captured" is easy to get for the wrong
    reason. Three guards, all of which must hold before the leak assertion
    means anything:

    1. ``caplog.set_level(logging.DEBUG)`` is applied to the ROOT logger only,
       never to the selenium logger. Setting it on the selenium logger would
       overwrite the clamp under test and the assertion would fail for a
       bookkeeping reason rather than pass for a vacuous one -- either way it
       would stop measuring the clamp.
    2. The selenium logger is reset to NOTSET and its *effective* level is
       asserted to be DEBUG BEFORE the clamp is applied. That proves the leak
       would genuinely be live in this test's logging configuration, so the
       clamp is the only thing suppressing it.
    3. An unclamped control logger emits a DEBUG record from inside the same
       mocked call. It MUST be captured. That proves caplog's DEBUG capture
       path is live end-to-end, so the selenium record's absence is real
       suppression and not a dead handler.
    """

    _SELENIUM_LOGGER = "selenium.webdriver.remote.remote_connection"
    _CONTROL_LOGGER = "tests.control.unclamped_debug_logger"

    @pytest.fixture
    def selenium_logger(self):
        """Yield the real selenium logger, restoring its level afterwards."""
        target = logging.getLogger(self._SELENIUM_LOGGER)
        original_level = target.level
        yield target
        target.setLevel(original_level)

    async def test_debug_log_level_does_not_leak_access_key(self, caplog, selenium_logger):
        access_key = "zzDEBUGLEAKkey99"  # pragma: allowlist secret
        hub_url = f"https://someuser:{access_key}@hub-cloud.browserstack.com/wd/hub"
        client = AppiumClient(appium_url=hub_url, capabilities={}, platform="android")

        control_logger = logging.getLogger(self._CONTROL_LOGGER)
        control_logger.setLevel(logging.NOTSET)

        mock_driver = MagicMock()
        mock_driver.session_id = "sess-debug"

        def fake_remote(url, options=None, client_config=None):
            """Emit what selenium 4.x emits at remote_connection.py:404.

            Verified against a real ``appium.webdriver.Remote`` driven at an
            unreachable loopback hub carrying inline userinfo, with the root
            logger at DEBUG: the only logger in the whole dependency chain that
            reproduced the access key was this one, with this call shape.
            """
            selenium_logger.debug("%s %s %s", "POST", f"{url}/session", "{'capabilities': {}}")
            control_logger.debug("control record proving DEBUG capture is live")
            return mock_driver

        appium_module = MagicMock()
        appium_module.webdriver.Remote = fake_remote

        with caplog.at_level(logging.DEBUG):  # ROOT only -- see guard (1)
            selenium_logger.setLevel(logging.NOTSET)  # guard (2): no residue
            assert selenium_logger.getEffectiveLevel() == logging.DEBUG, (
                "guard failed: the selenium logger must be DEBUG-live before the "
                "clamp, or this test proves nothing"
            )

            clamp_credential_leaking_loggers()
            assert selenium_logger.level >= logging.INFO, "clamp did not raise the logger's level"

            with (
                patch("src.services.appium_client.asyncio.get_running_loop") as mock_loop_fn,
                patch.dict(
                    "sys.modules",
                    {
                        "appium": appium_module,
                        "appium.webdriver": appium_module.webdriver,
                        "appium.options": MagicMock(),
                        "appium.options.android": MagicMock(UiAutomator2Options=MagicMock()),
                        "appium.options.ios": MagicMock(XCUITestOptions=MagicMock()),
                    },
                ),
            ):
                loop = MagicMock()

                async def run_executor(_executor, func):
                    return func()

                loop.run_in_executor = AsyncMock(side_effect=run_executor)
                mock_loop_fn.return_value = loop

                await client.connect("/tmp/app.apk")

        # guard (3): the capture path really does see DEBUG records.
        assert any(r.name == self._CONTROL_LOGGER for r in caplog.records), (
            "caplog captured no DEBUG record at all -- the leak assertion below "
            "would pass vacuously"
        )
        # The clamped logger contributed nothing...
        assert not [r for r in caplog.records if r.name == self._SELENIUM_LOGGER]
        # ...so the access key is in no record, and in no rendered line.
        for record in caplog.records:
            assert access_key not in record.getMessage()
        assert access_key not in caplog.text
        # Our own INFO line still survives, redacted.
        assert "hub-cloud.browserstack.com" in caplog.text


class TestAsgiWiresTheClamp:
    """The clamp is worthless unless the app's logging setup actually calls it."""

    def test_importing_asgi_clamps_the_selenium_logger(self):
        import src.asgi  # noqa: F401 -- import side effect IS the thing under test

        assert (
            logging.getLogger("selenium.webdriver.remote.remote_connection").level >= logging.INFO
        )
