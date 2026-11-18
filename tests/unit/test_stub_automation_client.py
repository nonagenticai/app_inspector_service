"""Unit tests for StubAutomationClient — offline 2-screen backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.automation_client import AutomationClient
from src.services.stub_automation_client import StubAutomationClient


@pytest.mark.asyncio
async def test_stub_implements_automation_client_protocol() -> None:
    client = StubAutomationClient()
    # Protocol is runtime-checkable.
    assert isinstance(client, AutomationClient)


@pytest.mark.asyncio
async def test_stub_connect_is_noop() -> None:
    client = StubAutomationClient()
    await client.connect("/dev/null/whatever.apk")
    assert client._connected is True


@pytest.mark.asyncio
async def test_stub_home_then_settings_progression() -> None:
    """Tap on home advances to settings; settings has no clickables."""
    client = StubAutomationClient()
    await client.connect("/dev/null/x")

    # Home screen.
    home_xml = await client.get_page_source()
    assert "<hierarchy>" in home_xml
    assert "Open settings" in home_xml
    home_clickable = await client.find_clickable_elements()
    assert len(home_clickable) == 1
    assert home_clickable[0]["text"] == "Open settings"

    # Tap → settings.
    await client.tap(home_clickable[0]["x"], home_clickable[0]["y"])
    settings_xml = await client.get_page_source()
    assert "Settings" in settings_xml
    assert "Version 1.0.0" in settings_xml
    assert (await client.find_clickable_elements()) == []

    # back() returns to home.
    await client.back()
    assert "Open settings" in await client.get_page_source()


@pytest.mark.asyncio
async def test_stub_screenshot_writes_real_png(tmp_path: Path) -> None:
    """Screenshot writes a valid 1x1 PNG so storage_service has bytes to upload."""
    client = StubAutomationClient()
    target = tmp_path / "shot.png"
    result = await client.screenshot(str(target))
    assert result == str(target)
    assert target.exists()
    data = target.read_bytes()
    # PNG magic + IHDR check.
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in data[:20]


@pytest.mark.asyncio
async def test_stub_activity_changes_with_screen() -> None:
    client = StubAutomationClient()
    await client.connect("x")
    assert await client.get_current_activity() == "com.stub.MainActivity"
    await client.tap(0, 0)
    assert await client.get_current_activity() == "com.stub.SettingsActivity"


@pytest.mark.asyncio
async def test_stub_recording_lifecycle_returns_empty_bytes() -> None:
    client = StubAutomationClient()
    await client.start_recording()
    data = await client.stop_recording()
    assert data == b""
