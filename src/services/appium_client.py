"""Appium WebDriver client wrapper for mobile app automation.

Wraps synchronous Appium calls in async executors for non-blocking usage.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from functools import partial
from pathlib import Path

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.client_config import ClientConfig

from src.services.automation_client import ScreenshotCaptureError
from src.utils.redaction import redact_url_credentials

logger = logging.getLogger(__name__)

# Seconds Selenium's HTTP layer will wait on the Appium hub. Without this,
# ``webdriver.Remote`` blocks forever inside ``run_in_executor``: cancelling the
# awaiting coroutine does NOT free the worker thread, so a black-holed hub
# permanently consumes an executor slot. The bound has to live at the HTTP
# client, not at the await.
DEFAULT_APPIUM_CLIENT_TIMEOUT = 300


class AppiumClient:
    """Async wrapper around Appium WebDriver for mobile app interaction."""

    def __init__(
        self,
        appium_url: str = "http://localhost:4723",
        capabilities: dict | None = None,
        platform: str = "android",
        connect_timeout: int = DEFAULT_APPIUM_CLIENT_TIMEOUT,
    ) -> None:
        self._appium_url = appium_url
        self._capabilities = capabilities or {}
        self._platform = platform
        self._connect_timeout = connect_timeout
        self._driver = None

    async def connect(self, app_path: str) -> None:
        """Start an Appium session with the given app binary.

        Args:
            app_path: Local or farm-uploaded app path/URL.
        """
        from appium import webdriver
        from appium.options.android import UiAutomator2Options
        from appium.options.ios import XCUITestOptions

        if self._platform == "ios":
            options = XCUITestOptions()
            options.app = app_path
        else:
            options = UiAutomator2Options()
            options.app = app_path

        # Apply extra capabilities
        for key, value in self._capabilities.items():
            options.set_capability(key, value)

        # BrowserStack hub URLs embed `user:access_key@` (see
        # DeviceFarmClient.get_appium_url) — log the redacted form only. The
        # unredacted URL is still what gets handed to webdriver.Remote below.
        logger.info(
            "Connecting to Appium at %s (platform=%s)",
            redact_url_credentials(self._appium_url),
            self._platform,
        )
        t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        # ClientConfig carries the socket/read timeout into Selenium's urllib3
        # pool. remote_server_addr keeps the credentialed URL where it belongs
        # (the request), never in a log line.
        client_config = ClientConfig(
            remote_server_addr=self._appium_url,
            timeout=self._connect_timeout,
        )
        self._driver = await loop.run_in_executor(
            None,
            partial(
                webdriver.Remote,
                self._appium_url,
                options=options,
                client_config=client_config,
            ),
        )
        logger.info(
            "Appium session started: %s (%.1fs)",
            self._driver.session_id,
            time.monotonic() - t0,
        )

    async def disconnect(self) -> None:
        """End the Appium session."""
        if self._driver:
            driver = self._driver
            self._driver = None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, driver.quit)

    async def get_page_source(self) -> str:
        """Get the XML page source of the current screen."""
        if not self._driver:
            raise RuntimeError("Not connected")
        driver = self._driver
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: driver.page_source)

    async def screenshot(self, path: str) -> str:
        """Take a screenshot and save to path. Returns the path.

        ``save_screenshot`` returns ``False`` on a write failure rather than
        raising, so its bool must be checked. Returning ``path`` unconditionally
        would hand the caller a non-empty path to a file that was never written.
        """
        if not self._driver:
            raise RuntimeError("Not connected")
        driver = self._driver
        loop = asyncio.get_running_loop()
        target = Path(path)
        await loop.run_in_executor(None, partial(target.parent.mkdir, parents=True, exist_ok=True))
        ok = await loop.run_in_executor(None, partial(driver.save_screenshot, path))
        if ok is False:
            raise ScreenshotCaptureError(f"appium save_screenshot returned False for {path}")
        if not target.is_file() or target.stat().st_size == 0:
            raise ScreenshotCaptureError(
                f"appium save_screenshot reported success but wrote no file at {path}"
            )
        return path

    async def find_clickable_elements(self) -> list[dict]:
        """Find all clickable/tappable elements on screen."""
        if not self._driver:
            return []
        driver = self._driver
        loop = asyncio.get_running_loop()

        def _find() -> list[dict]:
            results = []
            if self._platform == "android":
                elements = driver.find_elements("xpath", "//*[@clickable='true']")
            else:
                elements = driver.find_elements("xpath", "//*[@accessible='true']")

            for elem in elements:
                try:
                    results.append(
                        {
                            "text": elem.get_attribute("text") or elem.get_attribute("label") or "",
                            "content_desc": elem.get_attribute("content-desc") or "",
                            "resource_id": elem.get_attribute("resource-id") or "",
                            "tag": elem.tag_name,
                            "location": elem.location,
                            "size": elem.size,
                        }
                    )
                except (OSError, RuntimeError, WebDriverException) as exc:
                    logger.warning("Failed to extract element attributes: %s", exc)
            return results

        return await loop.run_in_executor(None, _find)

    async def tap(self, x: int, y: int) -> None:
        """Tap at coordinates."""
        if not self._driver:
            raise RuntimeError("Not connected")
        driver = self._driver
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            partial(driver.tap, [(x, y)]),
        )

    async def back(self) -> None:
        """Press back button."""
        if not self._driver:
            raise RuntimeError("Not connected")
        driver = self._driver
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, driver.back)

    async def get_current_activity(self) -> str:
        """Get current activity/screen name."""
        if not self._driver:
            return ""
        driver = self._driver
        loop = asyncio.get_running_loop()
        try:
            if self._platform == "android":
                return await loop.run_in_executor(None, lambda: driver.current_activity or "")
            return ""
        except OSError, RuntimeError, WebDriverException:
            return ""

    async def start_recording(self) -> None:
        """Start screen recording."""
        if not self._driver:
            return
        driver = self._driver
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, driver.start_recording_screen)

    async def stop_recording(self) -> bytes:
        """Stop recording and return video bytes."""
        if not self._driver:
            return b""
        driver = self._driver
        loop = asyncio.get_running_loop()
        b64_data = await loop.run_in_executor(None, driver.stop_recording_screen)
        return base64.b64decode(b64_data)

    @property
    def session_id(self) -> str | None:
        return self._driver.session_id if self._driver else None
