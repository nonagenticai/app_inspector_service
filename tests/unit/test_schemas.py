"""Tests for API schema serialization."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import SecretStr, ValidationError

from src.api.constants import DEFAULT_MAX_SCREENS
from src.api.schemas import (
    AccessibilityIssueInfo,
    AsyncJobResponse,
    CompareRequest,
    ContentSectionInfo,
    DeviceFarmConfig,
    FarmDevicesResponse,
    InspectionResultResponse,
    InspectRequest,
    JobStatusResponse,
    NavigationEdge,
    ScreenInfo,
    ScreenPreview,
    ScreenSelectionRequest,
    ScreenSelectionResponse,
    UIComponentInfo,
)


class TestInspectRequest:
    def test_minimal(self):
        req = InspectRequest(app_path="/tmp/test.apk")
        assert req.app_path == "/tmp/test.apk"
        assert req.platform is None
        assert req.automation_backend == "appium"
        assert req.max_screens == DEFAULT_MAX_SCREENS
        assert req.enable_recording is False
        assert req.require_selection is False

    def test_full(self):
        req = InspectRequest(
            app_path="https://example.com/test.apk",
            platform="android",
            automation_backend="maestro",
            max_screens=50,
            device_farm=DeviceFarmConfig(
                provider="browserstack",
                browserstack_username="bs-user",
                browserstack_access_key=SecretStr("bs-key"),  # pragma: allowlist secret
            ),
            enable_recording=True,
            require_selection=True,
        )
        assert req.platform == "android"
        assert req.automation_backend == "maestro"
        assert req.device_farm.provider == "browserstack"

    def test_max_screens_validation(self):
        with pytest.raises(ValidationError):
            InspectRequest(app_path="/tmp/test.apk", max_screens=0)
        with pytest.raises(ValidationError):
            InspectRequest(app_path="/tmp/test.apk", max_screens=101)


class TestScreenInfo:
    def test_defaults(self):
        screen = ScreenInfo(screen_id="s1", screen_type="login")
        assert screen.interactive_elements == 0
        assert screen.accessibility_score is None  # absent != a perfect score
        assert screen.has_forms is False
        assert screen.ui_components == []

    def test_full_screen(self):
        screen = ScreenInfo(
            screen_id="s1",
            screen_type="content",
            accessibility_issues=[AccessibilityIssueInfo(rule="missing_desc", severity="warning")],
            ui_components=[UIComponentInfo(component_type="tab_bar", tag="UITabBar")],
            content_sections=[ContentSectionInfo(heading="Header", element_count=5)],
        )
        assert len(screen.accessibility_issues) == 1
        assert len(screen.ui_components) == 1


class TestInspectionResultResponse:
    def test_defaults(self):
        result = InspectionResultResponse(job_id="j1", status="completed")
        assert result.overall_accessibility_score is None  # nothing measured
        assert result.estimated_purpose is None
        assert result.recording_path is None
        assert result.screens == []


class TestJobStatusResponse:
    def test_serialization(self):
        resp = JobStatusResponse(
            job_id="j1",
            status="completed",
            screens_discovered=5,
            result_url="/api/inspect/j1/result",
        )
        data = resp.model_dump()
        assert data["job_id"] == "j1"
        assert data["screens_discovered"] == 5


class TestNavigationEdge:
    def test_defaults(self):
        edge = NavigationEdge(from_screen="s1", to_screen="s2")
        assert edge.action == "tap"
        assert edge.element_label is None


class TestCompareRequest:
    def test_required_fields(self):
        req = CompareRequest(baseline_job_id="j1", comparison_job_id="j2")
        assert req.baseline_job_id == "j1"


class TestScreenSelectionSchemas:
    def test_selection_request(self):
        req = ScreenSelectionRequest(selected_screen_ids=["s1", "s2"])
        assert len(req.selected_screen_ids) == 2

    def test_screen_preview(self):
        preview = ScreenPreview(screen_id="s1", screen_type="login")
        assert preview.selected is True
        assert preview.text_preview is None


class TestLiteralValidation:
    def test_invalid_platform_rejected(self):
        with pytest.raises(ValidationError):
            InspectRequest(app_path="/tmp/test.apk", platform=cast(Any, "windows"))

    def test_valid_platform_accepted(self):
        req = InspectRequest(app_path="/tmp/test.apk", platform="ios")
        assert req.platform == "ios"

    def test_invalid_automation_backend_rejected(self):
        with pytest.raises(ValidationError):
            InspectRequest(app_path="/tmp/test.apk", automation_backend=cast(Any, "selenium"))

    def test_invalid_provider_rejected(self):
        with pytest.raises(ValidationError):
            DeviceFarmConfig(provider=cast(Any, "aws_device_farm"))

    def test_valid_provider_accepted(self):
        cfg = DeviceFarmConfig(provider="docker_android")
        assert cfg.provider == "docker_android"


class TestMaxLengthConstraints:
    def test_app_path_too_long_rejected(self):
        with pytest.raises(ValidationError):
            InspectRequest(app_path="x" * 2049)

    def test_app_path_at_limit_accepted(self):
        req = InspectRequest(app_path="x" * 2048)
        assert len(req.app_path) == 2048

    def test_device_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            DeviceFarmConfig(device_name="d" * 257)

    def test_os_version_too_long_rejected(self):
        with pytest.raises(ValidationError):
            DeviceFarmConfig(os_version="v" * 65)


class TestTypedResponseModels:
    def test_farm_devices_response(self):
        resp = FarmDevicesResponse(provider="local", devices=[{"name": "pixel"}])
        assert resp.provider == "local"
        assert len(resp.devices) == 1

    def test_screen_selection_response(self):
        resp = ScreenSelectionResponse(status="completed", selected_count=3)
        assert resp.status == "completed"
        assert resp.selected_count == 3


class TestResponseLiteralValidation:
    """Verify invalid status values are rejected on response models."""

    def test_async_job_response_invalid_status(self):
        with pytest.raises(ValidationError):
            AsyncJobResponse(job_id="j1", status=cast(Any, "invalid"), poll_url="/api/inspect/j1")

    def test_job_status_response_invalid_status(self):
        with pytest.raises(ValidationError):
            JobStatusResponse(job_id="j1", status=cast(Any, "unknown"))

    def test_inspection_result_invalid_status(self):
        with pytest.raises(ValidationError):
            InspectionResultResponse(job_id="j1", status=cast(Any, "invalid"))

    def test_screen_selection_response_invalid_status(self):
        with pytest.raises(ValidationError):
            ScreenSelectionResponse(status=cast(Any, "invalid"), selected_count=0)

    @pytest.mark.parametrize(
        "status",
        ["pending", "processing", "completed", "failed", "awaiting_selection"],
    )
    def test_all_valid_statuses_accepted(self, status):
        resp = JobStatusResponse(job_id="j1", status=status)
        assert resp.status == status


class TestResponseNumericConstraints:
    """Verify numeric field constraints on response models."""

    def test_negative_element_count_rejected(self):
        with pytest.raises(ValidationError):
            ContentSectionInfo(heading="h", element_count=-1)

    def test_negative_interactive_elements_rejected(self):
        with pytest.raises(ValidationError):
            ScreenInfo(screen_id="s1", screen_type="login", interactive_elements=-1)

    def test_accessibility_score_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            ScreenInfo(screen_id="s1", screen_type="login", accessibility_score=-1)

    def test_accessibility_score_above_100_rejected(self):
        with pytest.raises(ValidationError):
            ScreenInfo(screen_id="s1", screen_type="login", accessibility_score=101)

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            UIComponentInfo(component_type="button", confidence=-0.1)

    def test_confidence_above_one_rejected(self):
        with pytest.raises(ValidationError):
            UIComponentInfo(component_type="button", confidence=1.1)

    def test_text_similarity_above_one_rejected(self):
        from src.api.schemas import ScreenChangeInfo

        with pytest.raises(ValidationError):
            ScreenChangeInfo(
                screen_id_before="s1",
                screen_id_after="s2",
                screen_type_before="login",
                screen_type_after="login",
                text_similarity=1.1,
            )

    def test_negative_screens_discovered_rejected(self):
        with pytest.raises(ValidationError):
            InspectionResultResponse(job_id="j1", status="completed", screens_discovered=-1)

    def test_negative_inspection_time_rejected(self):
        with pytest.raises(ValidationError):
            InspectionResultResponse(job_id="j1", status="completed", inspection_time_seconds=-1.0)

    def test_negative_selected_count_rejected(self):
        with pytest.raises(ValidationError):
            ScreenSelectionResponse(status="completed", selected_count=-1)

    def test_overall_accessibility_score_above_100_rejected(self):
        with pytest.raises(ValidationError):
            InspectionResultResponse(
                job_id="j1", status="completed", overall_accessibility_score=101
            )


class TestResponseMaxLength:
    """Verify max_length constraints on response model string fields."""

    def test_job_id_too_long_rejected(self):
        with pytest.raises(ValidationError):
            AsyncJobResponse(job_id="x" * 257, status="pending", poll_url="/api/inspect/j1")

    def test_error_too_long_rejected(self):
        with pytest.raises(ValidationError):
            JobStatusResponse(job_id="j1", status="failed", error="x" * 4097)

    def test_text_content_too_long_rejected(self):
        with pytest.raises(ValidationError):
            ScreenInfo(screen_id="s1", screen_type="login", text_content="x" * 8193)

    def test_poll_url_too_long_rejected(self):
        with pytest.raises(ValidationError):
            AsyncJobResponse(job_id="j1", status="pending", poll_url="x" * 2049)


class TestScreenshotArtifactHonesty:
    """A completed result must not look identical with and without screenshots.

    Before this, `ScreenInfo.required` was `[screen_id, screen_type]` and the
    top level `[job_id, status]`, so a completed inspection with N screens and
    ZERO screenshots validated cleanly -- a non-empty structure of empty values.
    That is what let the downstream all-text-card bug ship green.
    """

    def test_screen_without_screenshot_is_explicitly_marked(self) -> None:
        screen = ScreenInfo(screen_id="s1", screen_type="login")
        assert screen.screenshot_path is None
        # Absence is UNMEASURED, never silently promoted to success.
        assert screen.screenshot_status == "unknown"

    def test_captured_claim_requires_a_path(self) -> None:
        with pytest.raises(ValidationError):
            ScreenInfo(screen_id="s1", screen_type="login", screenshot_status="captured")

    def test_failed_capture_is_representable(self) -> None:
        screen = ScreenInfo(screen_id="s1", screen_type="login", screenshot_status="failed")
        assert screen.screenshot_path is None
        assert screen.screenshot_status == "failed"

    def test_completed_result_with_screens_and_no_screenshots_is_degraded(self) -> None:
        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[
                ScreenInfo(screen_id="s1", screen_type="login", screenshot_status="failed"),
                ScreenInfo(screen_id="s2", screen_type="home", screenshot_status="failed"),
            ],
        )
        assert result.screenshots_captured == 0
        assert result.artifacts_degraded is True

    def test_fully_captured_result_is_not_degraded(self) -> None:
        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[
                ScreenInfo(
                    screen_id="s1",
                    screen_type="login",
                    screenshot_path="screenshots/screen_0.png",
                    screenshot_status="captured",
                ),
            ],
        )
        assert result.screenshots_captured == 1
        assert result.artifacts_degraded is False

    def test_partial_capture_is_degraded(self) -> None:
        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[
                ScreenInfo(
                    screen_id="s1",
                    screen_type="login",
                    screenshot_path="screenshots/screen_0.png",
                    screenshot_status="captured",
                ),
                ScreenInfo(screen_id="s2", screen_type="home", screenshot_status="failed"),
            ],
        )
        assert result.screenshots_captured == 1
        assert result.artifacts_degraded is True

    def test_derived_counts_cannot_be_overstated_by_the_producer(self) -> None:
        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[ScreenInfo(screen_id="s1", screen_type="login", screenshot_status="failed")],
            screenshots_captured=99,
            artifacts_degraded=False,
        )
        assert result.screenshots_captured == 0
        assert result.artifacts_degraded is True

    def test_screenshot_stays_optional_for_legitimately_degraded_runs(self) -> None:
        """app_inspect fails ~24% of the time on missing input; a hard
        `screenshot_path` requirement would reject those runs outright."""
        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[ScreenInfo(screen_id="s1", screen_type="login", screenshot_status="failed")],
        )
        assert result.status == "completed"
