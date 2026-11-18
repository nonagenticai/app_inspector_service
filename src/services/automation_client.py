"""Automation client protocol for mobile app interaction."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class AutomationConnectError(RuntimeError):
    """Raised when a backend could not install/launch the app under inspection.

    Subclasses ``RuntimeError`` so the existing ``except (OSError, RuntimeError,
    ...)`` handlers on the job path treat it as a normal job failure. It exists so
    a backend can never report a successful ``connect()`` for an app it did not
    actually put on a device — a job that then "completes" with an empty screen
    graph is a measurement gap dressed up as a result.
    """


class ScreenshotCaptureError(RuntimeError):
    """A screenshot capture did not produce a readable file on disk.

    Raised instead of returning the requested path so a failed capture can
    never hand a caller a non-empty path to a file that was never written.
    Subclasses ``RuntimeError`` so existing ``except (OSError, RuntimeError,
    WebDriverException)`` capture guards keep working.
    """


@runtime_checkable
class AutomationClient(Protocol):
    """Protocol defining the interface for automation backends.

    Both AppiumClient and MaestroClient implement this interface.
    """

    async def connect(self, app_path: str) -> None:
        """Start a session with the given app."""
        ...

    async def disconnect(self) -> None:
        """End the session."""
        ...

    async def get_page_source(self) -> str:
        """Get the XML page source of the current screen."""
        ...

    async def screenshot(self, path: str) -> str:
        """Take a screenshot and save to ``path``; return ``path``.

        Implementations MUST verify the file actually landed and raise
        :class:`ScreenshotCaptureError` otherwise. Returning the path is a
        positive assertion that a readable artifact exists at it.
        """
        ...

    async def find_clickable_elements(self) -> list[dict]:
        """Find all clickable/tappable elements on screen."""
        ...

    async def tap(self, x: int, y: int) -> None:
        """Tap at coordinates."""
        ...

    async def back(self) -> None:
        """Press back button."""
        ...

    async def get_current_activity(self) -> str:
        """Get current activity/screen name."""
        ...

    async def start_recording(self) -> None:
        """Start screen recording."""
        ...

    async def stop_recording(self) -> bytes:
        """Stop recording and return video bytes."""
        ...
