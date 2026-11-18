"""Request/response Pydantic schemas for the app inspector API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from src.api.constants import DEFAULT_MAX_SCREENS

JobStatus = Literal["pending", "processing", "completed", "failed", "awaiting_selection"]

# Explicit per-screen screenshot capture outcome.
#
# ``screenshot_path`` alone cannot distinguish "we captured nothing" from
# "we captured everything" -- both render as a well-formed screen object, which
# is how an all-text-card app_review video shipped on a clean green run
# (w2v_orchestrator #938). This field makes a screenshot-less screen EXPLICIT
# without making the screenshot itself mandatory: app_inspect legitimately
# degrades (~24% of runs fail on missing input) and a required
# ``screenshot_path`` would fail those runs outright.
#
#   captured      -- a readable artifact exists at ``screenshot_path``
#   failed        -- a capture was attempted and did not land (path is null)
#   not_requested -- no capture was asked for
#   unknown       -- produced before this field existed; UNMEASURED, not success
ScreenshotStatus = Literal["captured", "failed", "not_requested", "unknown"]

# Why a result is flagged ``artifacts_degraded``. A bare boolean cannot tell a
# consumer whether a crawl found nothing or never ran -- both render as
# ``screens_discovered == 0`` on an otherwise well-formed payload.
#
#   no_screens_discovered            -- ZERO screens. The emptiest possible
#                                       result; "found nothing" and "never ran"
#                                       are indistinguishable from here, so the
#                                       consumer must treat it as UNMEASURED.
#   no_screenshots_captured          -- screens exist, NONE carries an artifact
#   partial_screenshot_capture_failure -- some captured, at least one failed
DegradedReason = Literal[
    "no_screens_discovered",
    "no_screenshots_captured",
    "partial_screenshot_capture_failure",
]

# --- Request Schemas ---


class DeviceFarmConfig(BaseModel):
    """Device farm configuration for inspection."""

    provider: Literal["local", "docker_android", "browserstack"] = Field(
        default="local", description="Device farm: local, docker_android, browserstack"
    )
    appium_url: str | None = Field(None, max_length=2048, description="Appium server URL override")
    device_name: str | None = Field(None, max_length=256, description="Target device name")
    os_version: str | None = Field(None, max_length=64, description="Target OS version")
    real_device: bool = Field(default=False, description="Use real device vs emulator")
    # BrowserStack credentials travel per request (w2v P5 tenant ``device_farm``
    # slot: the orchestrator maps ``options.username`` -> username and the slot
    # ``secret`` -> access_key). They live in memory for the life of the job's
    # background task ONLY -- never written to ``inspection_jobs``, never logged,
    # never echoed in a response. SecretStr keeps the key out of repr/str/JSON.
    browserstack_username: str | None = Field(
        None,
        min_length=1,
        max_length=256,
        description="BrowserStack username (required when provider=browserstack)",
    )
    browserstack_access_key: SecretStr | None = Field(
        None,
        min_length=1,
        max_length=256,
        description="BrowserStack access key (required when provider=browserstack)",
    )

    @model_validator(mode="after")
    def _check_browserstack_credentials(self) -> DeviceFarmConfig:
        has_user = self.browserstack_username is not None
        has_key = self.browserstack_access_key is not None
        if self.provider == "browserstack":
            if not (has_user and has_key):
                raise ValueError(
                    "browserstack_username and browserstack_access_key are both "
                    "required when provider='browserstack'"
                )
        elif has_user or has_key:
            raise ValueError(
                "browserstack_username/browserstack_access_key are only allowed "
                "when provider='browserstack'"
            )
        return self


class InspectRequest(BaseModel):
    """Request to start a mobile app inspection."""

    app_path: str = Field(
        ...,
        max_length=2048,
        description=(
            "App binary location. Accepts http:// and https:// URLs (SSRF-validated "
            "by the acquisition service) and minio:// URIs, which must name the "
            "single configured bucket (ACQUISITION_ALLOWED_MINIO_BUCKET) and an "
            "object key under your own tenant prefix '<tenant_id>/'. Other schemes "
            "(s3://, file://) are rejected. Local paths must reside within /app/data "
            "or /tmp/app_inspector (configurable via ACQUISITION_ALLOWED_LOCAL_PATHS). "
            "Path traversal ('..', percent-encoded or not) is rejected."
        ),
    )
    platform: Literal["ios", "android"] | None = Field(
        None, description="Platform: ios or android (auto-detected if omitted)"
    )
    automation_backend: Literal["appium", "maestro", "stub"] = Field(
        default="appium",
        description=(
            "Backend: appium (real Appium server), maestro (Maestro CLI), "
            "or stub (offline deterministic 2-screen client for CI/E2E)"
        ),
    )
    max_screens: int = Field(
        default=DEFAULT_MAX_SCREENS, ge=1, le=100, description="Max screens to discover"
    )
    device_farm: DeviceFarmConfig = Field(default_factory=DeviceFarmConfig)
    enable_recording: bool = Field(
        default=False, description="Enable screen recording during inspection"
    )
    require_selection: bool = Field(
        default=False, description="Require screen selection before completing"
    )


# --- Response Schemas ---


class AsyncJobResponse(BaseModel):
    """Response returned when an inspection job is accepted."""

    model_config = ConfigDict(str_strip_whitespace=True)

    job_id: str = Field(max_length=256, description="Unique job identifier")
    status: JobStatus = Field(default="pending", description="Job status")
    poll_url: str = Field(max_length=2048, description="URL to poll for status")


class AccessibilityIssueInfo(BaseModel):
    """An accessibility issue on a screen."""

    model_config = ConfigDict(str_strip_whitespace=True)

    rule: str = Field(max_length=512, description="Accessibility rule violated")
    severity: str = Field(max_length=512, description="Issue severity level")
    element_tag: str = Field(default="", max_length=512, description="Element tag name")
    element_text: str = Field(default="", max_length=4096, description="Element text content")
    message: str = Field(default="", max_length=4096, description="Issue description")


class ContentSectionInfo(BaseModel):
    """A content section on a screen."""

    model_config = ConfigDict(str_strip_whitespace=True)

    heading: str = Field(default="", max_length=512, description="Section heading")
    element_count: int = Field(default=0, ge=0, description="Number of elements in section")
    has_interactive: bool = Field(
        default=False, description="Whether section has interactive elements"
    )


class InteractiveElementInfo(BaseModel):
    """Detail about an interactive element."""

    model_config = ConfigDict(str_strip_whitespace=True)

    tag: str = Field(max_length=512, description="Element tag name")
    text: str = Field(default="", max_length=4096, description="Element text")
    content_desc: str = Field(default="", max_length=512, description="Content description")
    resource_id: str = Field(default="", max_length=512, description="Resource identifier")
    clickable: bool = Field(default=False, description="Whether element is clickable")


class UIComponentInfo(BaseModel):
    """A detected UI component."""

    model_config = ConfigDict(str_strip_whitespace=True)

    component_type: str = Field(max_length=512, description="Component type")
    tag: str = Field(default="", max_length=512, description="Component tag")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Detection confidence")
    resource_id: str = Field(default="", max_length=512, description="Resource identifier")


class ScreenInfo(BaseModel):
    """Information about a discovered screen."""

    model_config = ConfigDict(str_strip_whitespace=True)

    screen_id: str = Field(max_length=256, description="Unique screen identifier")
    screen_type: str = Field(max_length=256, description="Classified screen type")
    activity_name: str | None = Field(
        default=None, max_length=512, description="Activity or view controller name"
    )
    text_content: str | None = Field(
        default=None, max_length=8192, description="Extracted text content"
    )
    interactive_elements: int = Field(default=0, ge=0, description="Number of interactive elements")
    accessibility_ids: list[str] = Field(
        default_factory=list, description="Accessibility identifiers"
    )
    screenshot_path: str | None = Field(
        default=None, max_length=2048, description="Path to screenshot"
    )
    screenshot_status: ScreenshotStatus = Field(
        default="unknown",
        description=(
            "Explicit screenshot capture outcome. Only 'captured' asserts an "
            "artifact exists; the default 'unknown' means unmeasured, NOT success."
        ),
    )
    headings: list[str] = Field(default_factory=list, description="Detected headings")
    has_forms: bool = Field(default=False, description="Whether screen contains forms")
    has_media: bool = Field(default=False, description="Whether screen contains media")
    content_sections: list[ContentSectionInfo] = Field(
        default_factory=list, description="Content sections"
    )
    accessibility_issues: list[AccessibilityIssueInfo] = Field(
        default_factory=list, description="Accessibility issues"
    )
    accessibility_score: int | None = Field(
        default=None, ge=0, le=100, description="Accessibility score (null = not measured)"
    )
    interactive_elements_detail: list[InteractiveElementInfo] = Field(
        default_factory=list,
        description="Interactive element details",
    )
    ui_components: list[UIComponentInfo] = Field(
        default_factory=list, description="Detected UI components"
    )
    navigation_failures: int = Field(
        default=0,
        ge=0,
        description=(
            "Taps/backs attempted FROM this screen that raised and were dropped "
            "during the crawl. A non-zero count means the navigation graph is "
            "INCOMPLETE for this screen: edges may be missing because the "
            "transition never happened, not because the app has none."
        ),
    )

    @model_validator(mode="after")
    def _screenshot_claim_must_have_a_path(self) -> ScreenInfo:
        """A 'captured' claim without a path is not representable.

        This is what makes the status trustworthy: a producer cannot report
        success while carrying nothing.
        """
        if self.screenshot_status == "captured" and not self.screenshot_path:
            raise ValueError("screenshot_status='captured' requires a non-empty screenshot_path")
        return self


class NavigationEdge(BaseModel):
    """A navigation edge in the screen graph."""

    model_config = ConfigDict(str_strip_whitespace=True)

    from_screen: str = Field(max_length=256, description="Source screen ID")
    to_screen: str = Field(max_length=256, description="Target screen ID")
    action: str = Field(default="tap", max_length=512, description="Navigation action")
    element_label: str | None = Field(
        default=None, max_length=512, description="Label of element triggering navigation"
    )


class InspectionResultResponse(BaseModel):
    """Full inspection result."""

    model_config = ConfigDict(str_strip_whitespace=True)

    job_id: str = Field(max_length=256, description="Unique job identifier")
    status: JobStatus = Field(description="Job status")
    platform: str | None = Field(default=None, max_length=256, description="Target platform")
    app_path: str | None = Field(default=None, max_length=2048, description="App binary path")
    screens: list[ScreenInfo] = Field(default_factory=list, description="Discovered screens")
    navigation_graph: list[NavigationEdge] = Field(
        default_factory=list, description="Navigation graph edges"
    )
    screens_discovered: int = Field(default=0, ge=0, description="Number of screens discovered")
    inspection_time_seconds: float | None = Field(
        default=None, ge=0.0, description="Total inspection duration"
    )
    error: str | None = Field(default=None, max_length=4096, description="Error message if failed")
    overall_accessibility_score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Overall accessibility score (null = nothing was measured)",
    )
    estimated_purpose: str | None = Field(
        default=None, max_length=8192, description="Inferred app purpose"
    )
    # Declared for the wire contract but NOT yet computed anywhere in src/ — the
    # inspection pipeline never assigns it, so it is always None on /result. A
    # heuristic populator is deferred (see HANDOFF app-inspector-fix). Do not
    # re-flag this as a wiring bug; it is a known, deliberate gap.
    design_system_summary: str | None = Field(
        default=None, max_length=8192, description="Design system summary"
    )
    recording_path: str | None = Field(
        default=None, max_length=2048, description="Path to session recording"
    )
    screenshots_captured: int = Field(
        default=0,
        ge=0,
        description=(
            "Screens carrying a screenshot reference. DERIVED from screens -- a "
            "producer cannot overstate it."
        ),
    )
    artifacts_degraded: bool = Field(
        default=False,
        description=(
            "True when this result is short on artifacts: NO screen was "
            "discovered at all, a screen reports a failed capture, or screens "
            "were discovered and NONE carries a screenshot. DERIVED -- a "
            "consumer can gate on this instead of inferring success from a "
            "well-formed but empty structure."
        ),
    )
    degraded_reason: DegradedReason | None = Field(
        default=None,
        description=(
            "WHY ``artifacts_degraded`` is true, or null when it is false. "
            "``screens_discovered == 0`` alone does not tell a consumer whether "
            "the crawl found nothing or never ran, and consumers were told to "
            "gate on the derived flag rather than re-derive it -- so the flag "
            "carries its own reason. DERIVED."
        ),
    )
    navigation_failures: int = Field(
        default=0,
        ge=0,
        description=(
            "Total tap/back attempts that raised and were dropped during the "
            "crawl, summed over ``screens``. Non-zero means the navigation "
            "graph is INCOMPLETE: a sparse or 0-edge graph here was not "
            "observed to be sparse, it was unmeasured. DERIVED."
        ),
    )

    @model_validator(mode="after")
    def _derive_artifact_signals(self) -> InspectionResultResponse:
        """Recompute the artifact signals from ``screens``.

        Derived rather than passed in so the fields cannot drift from, or lie
        about, the screens they summarise.

        ⚠️ This runs UNCONDITIONALLY, including for ``screens == []``. It used to
        sit inside ``if self.screens:``, which meant the emptiest possible
        result -- automation connected, page source was a blank/splash
        hierarchy, ``find_clickable_elements`` returned nothing, the crawl ended
        with zero screens -- kept both field DEFAULTS and reported
        ``artifacts_degraded=False``. The signal built to make
        "shape-complete, artifact-empty" visible was reporting the MOST
        degraded case as healthy. Note that the expression below already said
        so: for an empty list ``captured == 0`` is exactly True; only the guard
        stopped it running.
        """
        # Count REFERENCED artifacts, not self-reported successes: a path is
        # only ever set now when the capture verifiably landed (the producer
        # raises otherwise), and rows written before screenshot_status
        # existed report "unknown" without being falsely flagged.
        captured = sum(1 for s in self.screens if s.screenshot_path)
        any_failed = any(s.screenshot_status == "failed" for s in self.screens)
        nav_failures = sum(s.navigation_failures for s in self.screens)

        reason: DegradedReason | None
        if not self.screens:
            # "Found nothing" and "never ran" are indistinguishable from the
            # payload shape alone; naming the condition is what separates them.
            reason = "no_screens_discovered"
        elif captured == 0:
            reason = "no_screenshots_captured"
        elif any_failed:
            reason = "partial_screenshot_capture_failure"
        else:
            reason = None

        object.__setattr__(self, "screenshots_captured", captured)
        object.__setattr__(self, "navigation_failures", nav_failures)
        object.__setattr__(self, "artifacts_degraded", reason is not None)
        object.__setattr__(self, "degraded_reason", reason)
        return self


class JobStatusResponse(BaseModel):
    """Polling response for an inspection job's current status."""

    model_config = ConfigDict(str_strip_whitespace=True)

    job_id: str = Field(max_length=256, description="Unique job identifier")
    status: JobStatus = Field(description="Job status")
    screens_discovered: int = Field(default=0, ge=0, description="Number of screens discovered")
    created_at: str | None = Field(
        default=None, max_length=256, description="Job creation timestamp"
    )
    completed_at: str | None = Field(
        default=None, max_length=256, description="Job completion timestamp"
    )
    error: str | None = Field(default=None, max_length=4096, description="Error message if failed")
    result_url: str | None = Field(
        default=None, max_length=2048, description="URL to fetch full results"
    )


class DeviceFarmInfo(BaseModel):
    """Information about a device farm provider."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(max_length=256, description="Provider name")
    available: bool = Field(description="Whether provider is available")
    description: str | None = Field(
        default=None, max_length=1024, description="Provider description"
    )


# --- Comparison Schemas ---


class CompareRequest(BaseModel):
    """Request to compare two inspections."""

    baseline_job_id: str = Field(..., description="Job ID of the baseline inspection")
    comparison_job_id: str = Field(..., description="Job ID of the comparison inspection")


class ScreenChangeInfo(BaseModel):
    """Details about a changed screen."""

    model_config = ConfigDict(str_strip_whitespace=True)

    screen_id_before: str = Field(max_length=256, description="Screen ID in baseline")
    screen_id_after: str = Field(max_length=256, description="Screen ID in comparison")
    screen_type_before: str = Field(max_length=256, description="Screen type in baseline")
    screen_type_after: str = Field(max_length=256, description="Screen type in comparison")
    activity_name: str | None = Field(default=None, max_length=512, description="Activity name")
    text_similarity: float = Field(default=0.0, ge=0.0, le=1.0, description="Text similarity score")
    interactive_elements_before: int = Field(
        default=0, ge=0, description="Interactive elements in baseline"
    )
    interactive_elements_after: int = Field(
        default=0, ge=0, description="Interactive elements in comparison"
    )
    change_summary: str = Field(default="", max_length=8192, description="Summary of changes")


class NavigationChangeInfo(BaseModel):
    """A navigation edge change."""

    model_config = ConfigDict(str_strip_whitespace=True)

    from_screen: str = Field(max_length=256, description="Source screen ID")
    to_screen: str = Field(max_length=256, description="Target screen ID")
    action: str = Field(default="tap", max_length=512, description="Navigation action")
    element_label: str | None = Field(default=None, max_length=512, description="Element label")
    change_type: str = Field(default="", max_length=256, description="Type of change")


class InspectionDiffResponse(BaseModel):
    """Result of comparing two inspections."""

    model_config = ConfigDict(str_strip_whitespace=True)

    added_screens: list[ScreenInfo] = Field(
        default_factory=list, description="Screens added in comparison"
    )
    removed_screens: list[ScreenInfo] = Field(
        default_factory=list, description="Screens removed in comparison"
    )
    changed_screens: list[ScreenChangeInfo] = Field(
        default_factory=list, description="Changed screens"
    )
    unchanged_screen_ids: list[str] = Field(
        default_factory=list, description="Unchanged screen IDs"
    )
    navigation_changes: list[NavigationChangeInfo] = Field(
        default_factory=list, description="Navigation changes"
    )
    summary: str = Field(default="", max_length=8192, description="Comparison summary")


class FarmsListResponse(BaseModel):
    """Response listing all available device farm providers."""

    farms: list[DeviceFarmInfo]


# --- Screen Selection Schemas ---


class ScreenSelectionRequest(BaseModel):
    """Request to select screens from an inspection."""

    selected_screen_ids: list[str] = Field(..., description="List of screen IDs to include")


class ScreenPreview(BaseModel):
    """Screen preview for selection UI."""

    model_config = ConfigDict(str_strip_whitespace=True)

    screen_id: str = Field(max_length=256, description="Screen identifier")
    screen_type: str = Field(max_length=256, description="Screen type")
    activity_name: str | None = Field(default=None, max_length=512, description="Activity name")
    text_preview: str | None = Field(default=None, max_length=8192, description="Text preview")
    interactive_elements: int = Field(default=0, ge=0, description="Number of interactive elements")
    screenshot_path: str | None = Field(
        default=None, max_length=2048, description="Screenshot path"
    )
    selected: bool = Field(default=True, description="Whether screen is selected")


# --- Typed Response Models ---


class FarmDevicesResponse(BaseModel):
    """Response listing devices from a farm provider."""

    model_config = ConfigDict(str_strip_whitespace=True)

    provider: str = Field(max_length=256, description="Provider name")
    devices: list[dict[str, Any]] = Field(description="List of available devices")


class ScreenSelectionResponse(BaseModel):
    """Response after screen selection is saved."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: JobStatus = Field(description="Job status")
    selected_count: int = Field(ge=0, description="Number of selected screens")
