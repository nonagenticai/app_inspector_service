"""Maestro CLI client wrapper for mobile app automation."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from pathlib import Path

from src.api.constants import (
    MAESTRO_BACK_TIMEOUT,
    MAESTRO_DEFAULT_TIMEOUT,
    MAESTRO_HIERARCHY_TIMEOUT,
    MAESTRO_INSTALL_TIMEOUT,
    MAESTRO_LAUNCH_TIMEOUT,
    MAESTRO_RECORD_TIMEOUT,
    MAESTRO_SCREENSHOT_TIMEOUT,
    MAESTRO_TAP_TIMEOUT,
)
from src.services.automation_client import AutomationConnectError, ScreenshotCaptureError

logger = logging.getLogger(__name__)

# Element types considered interactive even without explicit clickable=true
_INTERACTIVE_TYPES = frozenset(
    {
        "android.widget.Button",
        "android.widget.ImageButton",
        "android.widget.CheckBox",
        "android.widget.RadioButton",
        "android.widget.Switch",
        "android.widget.ToggleButton",
        "android.widget.Spinner",
        "android.widget.EditText",
        "Button",
        "ImageButton",
        "CheckBox",
        "RadioButton",
        "Switch",
        "ToggleButton",
        "Spinner",
        "EditText",
        "XCUIElementTypeButton",
        "XCUIElementTypeTextField",
        "XCUIElementTypeSwitch",
        "XCUIElementTypeSlider",
        "XCUIElementTypeLink",
    }
)


class MaestroClient:
    """Async wrapper around Maestro CLI for mobile app interaction."""

    def __init__(self, platform: str = "android") -> None:
        self._platform = platform

    async def connect(self, app_path: str) -> None:
        """Install and launch the app using maestro CLI.

        ``_run_maestro`` does NOT raise on a non-zero returncode — it logs and
        RETURNS the rc — so both rcs must be checked here, exactly as
        :meth:`screenshot` already does. Discarding them made a total failure to
        install or launch indistinguishable from success:

          no device/emulator attached -> ``maestro install`` exits non-zero ->
          ``connect`` returns cleanly -> every later ``maestro hierarchy`` also
          exits non-zero -> :meth:`get_page_source` substitutes the synthetic
          ``<hierarchy />`` -> ``ScreenNavigator`` parses it fine and yields one
          empty screen -> ``InspectorService`` calls ``mark_completed`` +
          ``record_usage(inspections_completed=1)``.

        i.e. the caller was billed for, and shown, a ``completed`` inspection of
        an app that was never installed. Raising here routes the job through
        ``_process_inspection_bg``'s ``mark_failed`` instead, which is the truth.
        """
        logger.info("Maestro: installing and launching %s", app_path)
        # Install the app
        _out, install_err, install_rc = await self._run_maestro(
            "install", app_path, timeout=MAESTRO_INSTALL_TIMEOUT
        )
        if install_rc != 0:
            raise AutomationConnectError(
                f"maestro install failed (rc={install_rc}) for {app_path}: {install_err[:200]}"
            )
        # Launch the app
        _out, launch_err, launch_rc = await self._run_maestro(
            "launch", app_path, timeout=MAESTRO_LAUNCH_TIMEOUT
        )
        if launch_rc != 0:
            raise AutomationConnectError(
                f"maestro launch failed (rc={launch_rc}) for {app_path}: {launch_err[:200]}"
            )
        logger.info("Maestro: app launched successfully")

    async def disconnect(self) -> None:
        """No-op for Maestro -- nothing to tear down."""
        logger.debug("Maestro: disconnect (no-op)")

    async def get_page_source(self) -> str:
        """Get the XML page source by running maestro hierarchy."""
        stdout, _stderr, rc = await self._run_maestro(
            "hierarchy", timeout=MAESTRO_HIERARCHY_TIMEOUT
        )
        if rc != 0:
            logger.warning("Maestro hierarchy command failed (rc=%d)", rc)
            return "<hierarchy />"

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("Maestro hierarchy returned invalid JSON")
            return "<hierarchy />"

        return self._hierarchy_to_xml(data)

    async def screenshot(self, path: str) -> str:
        """Take a screenshot and save to path. Returns the path.

        ``_run_maestro`` does NOT raise on a non-zero returncode -- it logs and
        returns the rc -- so the rc must be checked here. Returning ``path``
        unconditionally would hand the caller a non-empty path to a file that
        was never written.
        """
        # Ensure parent directory exists
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _stdout, stderr, rc = await self._run_maestro(
            "screenshot", path, timeout=MAESTRO_SCREENSHOT_TIMEOUT
        )
        if rc != 0:
            raise ScreenshotCaptureError(
                f"maestro screenshot failed (rc={rc}) for {path}: {stderr[:200]}"
            )
        if not target.is_file() or target.stat().st_size == 0:
            raise ScreenshotCaptureError(
                f"maestro screenshot reported success but wrote no file at {path}"
            )
        return path

    async def find_clickable_elements(self) -> list[dict]:
        """Find all clickable/tappable elements on screen."""
        stdout, _stderr, rc = await self._run_maestro(
            "hierarchy", timeout=MAESTRO_HIERARCHY_TIMEOUT
        )
        if rc != 0:
            return []

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return []

        elements: list[dict] = []
        self._extract_clickable(data, elements)
        return elements

    async def tap(self, x: int, y: int) -> None:
        """Tap at coordinates."""
        await self._run_maestro("tap", str(x), str(y), timeout=MAESTRO_TAP_TIMEOUT)

    async def back(self) -> None:
        """Press back button."""
        await self._run_maestro("back", timeout=MAESTRO_BACK_TIMEOUT)

    async def get_current_activity(self) -> str:
        """Get current activity/screen name -- not supported by Maestro."""
        return ""

    async def start_recording(self) -> None:
        """Start screen recording (best-effort)."""
        try:
            await self._run_maestro("record", "start", timeout=MAESTRO_RECORD_TIMEOUT)
        except OSError, RuntimeError:
            logger.debug("Maestro: start_recording not supported, skipping")

    async def stop_recording(self) -> bytes:
        """Stop recording and return video bytes (best-effort)."""
        try:
            await self._run_maestro("record", "stop", timeout=MAESTRO_RECORD_TIMEOUT)
        except OSError, RuntimeError:
            logger.debug("Maestro: stop_recording not supported, skipping")
        return b""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_maestro(
        self, *args: str, timeout: int = MAESTRO_DEFAULT_TIMEOUT
    ) -> tuple[str, str, int]:
        """Execute a maestro CLI command via subprocess.

        Returns (stdout, stderr, returncode).
        Raises TimeoutError if the command exceeds *timeout* seconds.
        """
        cmd = ["maestro", *args]
        logger.debug("Running: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(
                f"Maestro command timed out after {timeout}s: {' '.join(cmd)}"
            ) from None

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            logger.warning(
                "Maestro command failed (rc=%d): %s\nstderr: %s",
                proc.returncode,
                " ".join(cmd),
                stderr[:500],
            )

        return stdout, stderr, proc.returncode or 0

    # ------------------------------------------------------------------
    # Hierarchy JSON -> XML conversion
    # ------------------------------------------------------------------

    def _hierarchy_to_xml(self, data: dict | list) -> str:
        """Convert Maestro JSON hierarchy to XML-like string for the parser."""
        lines: list[str] = ["<hierarchy>"]
        self._walk_json(data, lines, depth=1)
        lines.append("</hierarchy>")
        return "\n".join(lines)

    def _walk_json(self, node: dict | list, lines: list[str], depth: int) -> None:
        """Recursively walk the hierarchy and emit XML-like elements."""
        if isinstance(node, list):
            for child in node:
                self._walk_json(child, lines, depth)
            return
        if not isinstance(node, dict):
            return

        indent = "  " * depth
        tag = node.get("type", node.get("className", "element"))
        attrs: list[str] = []
        for key in ("text", "resource-id", "content-desc", "clickable", "enabled", "bounds"):
            val = node.get(key, "")
            if val:
                attrs.append(f'{key}="{html.escape(str(val), quote=True)}"')
        attrs_str = " ".join(attrs)

        children = node.get("children", [])
        if children:
            lines.append(f"{indent}<{tag} {attrs_str}>")
            for child in children:
                self._walk_json(child, lines, depth + 1)
            lines.append(f"{indent}</{tag}>")
        else:
            lines.append(f"{indent}<{tag} {attrs_str} />")

    # ------------------------------------------------------------------
    # Clickable element extraction
    # ------------------------------------------------------------------

    def _extract_clickable(self, node: dict | list, elements: list[dict]) -> None:
        """Recursively extract clickable elements from the hierarchy JSON."""
        if isinstance(node, list):
            for child in node:
                self._extract_clickable(child, elements)
            return
        if not isinstance(node, dict):
            return

        tag = node.get("type", node.get("className", "element"))
        clickable = str(node.get("clickable", "")).lower() == "true"
        is_interactive_type = tag in _INTERACTIVE_TYPES

        if clickable or is_interactive_type:
            bounds = node.get("bounds", "")
            location, size = self._parse_bounds(bounds)
            elements.append(
                {
                    "text": node.get("text", ""),
                    "content_desc": node.get("content-desc", ""),
                    "resource_id": node.get("resource-id", ""),
                    "tag": tag,
                    "location": location,
                    "size": size,
                }
            )

        for child in node.get("children", []):
            self._extract_clickable(child, elements)

    @staticmethod
    def _parse_bounds(bounds: str) -> tuple[dict, dict]:
        """Parse bounds string like '[0,100][200,300]' into location and size dicts."""
        try:
            # Format: [left,top][right,bottom]
            parts = bounds.replace("][", ",").strip("[]").split(",")
            if len(parts) == 4:
                left, top, right, bottom = (
                    int(parts[0]),
                    int(parts[1]),
                    int(parts[2]),
                    int(parts[3]),
                )
                return (
                    {"x": left, "y": top},
                    {"width": right - left, "height": bottom - top},
                )
        except ValueError, IndexError:
            pass
        return {"x": 0, "y": 0}, {"width": 0, "height": 0}
