"""Stub automation client — deterministic offline backend for CI/E2E.

Selected via ``automation_backend="stub"`` on an inspection job. The
client does **not** talk to Appium, Maestro, BrowserStack, or any
device farm. It returns a fixed 2-screen Android-style hierarchy so the
real screen discovery + accessibility + persistence pipeline runs
against synthetic data — which is the part that matters for the
orchestrator's ``app_review`` blueprint reaching ``completed``.

The flow it scripts is:

  1. Caller connects (no-op, no app needed).
  2. ``get_page_source()`` returns the "home" screen XML.
  3. ``find_clickable_elements()`` returns one button.
  4. Caller taps → internal counter advances to "settings".
  5. ``get_page_source()`` returns the "settings" screen XML.
  6. ``find_clickable_elements()`` returns [] (terminal).
  7. ``back()`` returns the home screen on next ``get_page_source``,
     letting the discoverer recover normally if it BFS-explores both.
  8. ``screenshot(path)`` writes a 1x1 PNG so storage_service has a
     real file to upload.

This is faithful in the sense that the real ``screen_navigator`` /
``xml_parser`` / ``accessibility_analyzer`` / ``screen_classifier`` /
``purpose_inferrer`` / ``storage_service`` / persistence chain runs
end-to-end. It is *not* faithful in any sense about real apps.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# A 1x1 transparent PNG — smallest valid PNG, enough to satisfy the
# storage layer and any downstream "screenshot exists" check.
_TINY_PNG: bytes = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
    b"\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0d"
    b"IDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x9e\xe5\xc6"
    b"\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_HOME_XML = """<hierarchy>
  <android.widget.FrameLayout resource-id="com.stub:id/root" bounds="[0,0][1080,1920]">
    <android.widget.TextView resource-id="com.stub:id/title" text="Stub App" bounds="[40,160][1040,260]" />
    <android.widget.Button resource-id="com.stub:id/open_settings" text="Open settings" content-desc="Open settings" clickable="true" enabled="true" bounds="[200,800][880,960]" />
  </android.widget.FrameLayout>
</hierarchy>"""

_SETTINGS_XML = """<hierarchy>
  <android.widget.LinearLayout resource-id="com.stub:id/settings_root" bounds="[0,0][1080,1920]">
    <android.widget.TextView resource-id="com.stub:id/settings_title" text="Settings" bounds="[40,160][1040,260]" />
    <android.widget.TextView resource-id="com.stub:id/version" text="Version 1.0.0" bounds="[40,300][1040,360]" />
    <android.widget.EditText resource-id="com.stub:id/email" text="" content-desc="Email" bounds="[40,500][1040,620]" />
  </android.widget.LinearLayout>
</hierarchy>"""

_HOME_CLICKABLE = [
    {
        "tag": "android.widget.Button",
        "text": "Open settings",
        "resource-id": "com.stub:id/open_settings",
        "content-desc": "Open settings",
        "bounds": "[200,800][880,960]",
        "x": 540,
        "y": 880,
    }
]

_SETTINGS_CLICKABLE: list[dict] = []


class StubAutomationClient:
    """Offline 2-screen automation backend for E2E.

    Implements the duck-typed ``AutomationClient`` protocol without
    importing it (avoids a circular import between
    ``automation_client.py`` and this module).
    """

    SCREEN_HOME = "home"
    SCREEN_SETTINGS = "settings"

    def __init__(self, platform: str = "android") -> None:
        self._platform = platform
        self._current = self.SCREEN_HOME
        self._connected = False
        self._recording = False

    async def connect(self, app_path: str) -> None:
        """No-op — the stub does not need an APK/IPA."""
        logger.info(
            "StubAutomationClient.connect(platform=%s, app_path=%r) — no-op",
            self._platform,
            app_path,
        )
        self._connected = True
        self._current = self.SCREEN_HOME

    async def disconnect(self) -> None:
        self._connected = False

    async def get_page_source(self) -> str:
        if self._current == self.SCREEN_SETTINGS:
            return _SETTINGS_XML
        return _HOME_XML

    async def screenshot(self, path: str) -> str:
        # Write a real (1x1) PNG so storage_service has bytes to persist.
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(_TINY_PNG)
        return path

    async def find_clickable_elements(self) -> list[dict]:
        if self._current == self.SCREEN_HOME:
            return list(_HOME_CLICKABLE)
        return list(_SETTINGS_CLICKABLE)

    async def tap(self, x: int, y: int) -> None:
        # Advance the deterministic state machine: any tap from home
        # navigates to settings (the only clickable on home).
        if self._current == self.SCREEN_HOME:
            self._current = self.SCREEN_SETTINGS

    async def back(self) -> None:
        if self._current == self.SCREEN_SETTINGS:
            self._current = self.SCREEN_HOME

    async def get_current_activity(self) -> str:
        if self._current == self.SCREEN_SETTINGS:
            return "com.stub.SettingsActivity"
        return "com.stub.MainActivity"

    async def start_recording(self) -> None:
        self._recording = True

    async def stop_recording(self) -> bytes:
        self._recording = False
        # Empty bytes is fine — _stop_recording() in inspector_service
        # treats falsy `video_data` as "no recording to persist".
        return b""
