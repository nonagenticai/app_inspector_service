"""Unit tests for ``_resolve_automation_backend`` env override."""

from __future__ import annotations

import pytest

from src.api.routes.inspections import _resolve_automation_backend


def test_no_env_returns_request_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_INSPECTOR_DEFAULT_BACKEND", raising=False)
    assert _resolve_automation_backend("appium") == "appium"
    assert _resolve_automation_backend("maestro") == "maestro"
    assert _resolve_automation_backend("stub") == "stub"


def test_env_overrides_default_appium(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the env is set, an unspecified backend (Pydantic default 'appium')
    is rewritten to the env value."""
    monkeypatch.setenv("APP_INSPECTOR_DEFAULT_BACKEND", "stub")
    assert _resolve_automation_backend("appium") == "stub"


def test_env_does_not_override_explicit_non_appium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``maestro`` / ``stub`` requests are NOT overridden."""
    monkeypatch.setenv("APP_INSPECTOR_DEFAULT_BACKEND", "stub")
    assert _resolve_automation_backend("maestro") == "maestro"
    assert _resolve_automation_backend("stub") == "stub"


def test_empty_env_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_INSPECTOR_DEFAULT_BACKEND", "")
    assert _resolve_automation_backend("appium") == "appium"
