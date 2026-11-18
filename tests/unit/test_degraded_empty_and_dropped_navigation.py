"""Two measurement gaps that reported themselves as healthy.

A. The emptiest possible result was flagged CLEAN. The whole
   ``artifacts_degraded`` / ``screenshots_captured`` derivation sat inside
   ``if self.screens:``, so a crawl that ended with ZERO screens kept both
   field defaults and answered ``artifacts_degraded=False`` — the signal built
   precisely to make "shape-complete, artifact-empty" visible reported the most
   degraded case as healthy. The irony that makes it unambiguous: the guarded
   expression is ``any_failed or captured == 0``, and for an empty list
   ``captured == 0`` is exactly True.

B. Navigation failures were dropped silently. Every tap/back that raised was
   logged and forgotten, so a partially-explored screen was byte-identical to a
   screen with nowhere to go — and purpose inference then described a 0-edge
   graph as having "simple, linear navigation" it had never observed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.api.routes._helpers import _build_screen_info
from src.api.schemas import InspectionResultResponse, ScreenInfo
from src.services.inspector_service import _serialize_screen
from src.services.screen_navigator import ScreenNavigator

# ---------------------------------------------------------------------------
# A. artifacts_degraded must fire on an EMPTY result
# ---------------------------------------------------------------------------


class TestEmptyResultIsDegraded:
    def test_completed_job_with_zero_screens_is_degraded(self) -> None:
        """The regression this file exists for. Fails without the guard fix."""
        result = InspectionResultResponse(job_id="j1", status="completed", screens=[])

        assert result.screens == []
        assert result.screenshots_captured == 0
        assert result.artifacts_degraded is True

    def test_zero_screens_carries_a_reason_not_just_a_flag(self) -> None:
        """``screens_discovered == 0`` alone cannot tell a consumer whether the
        crawl found nothing or never ran, so the flag names its own cause."""
        result = InspectionResultResponse(
            job_id="j1", status="completed", screens=[], screens_discovered=0
        )

        assert result.degraded_reason == "no_screens_discovered"

    def test_a_producer_cannot_claim_an_empty_result_is_clean(self) -> None:
        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[],
            artifacts_degraded=False,
            degraded_reason=None,
            screenshots_captured=7,
        )

        assert result.artifacts_degraded is True
        assert result.degraded_reason == "no_screens_discovered"
        assert result.screenshots_captured == 0

    def test_screens_but_failed_screenshot_still_degraded(self) -> None:
        """Unchanged behaviour: the pre-existing signal must keep working."""
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
        assert result.degraded_reason == "partial_screenshot_capture_failure"

    def test_screens_with_no_screenshots_at_all_is_degraded(self) -> None:
        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[ScreenInfo(screen_id="s1", screen_type="home", screenshot_status="failed")],
        )

        assert result.artifacts_degraded is True
        assert result.degraded_reason == "no_screenshots_captured"

    def test_fully_healthy_job_is_not_degraded(self) -> None:
        """The regression guard that matters most: widening the derivation must
        not start flagging good runs."""
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
                ScreenInfo(
                    screen_id="s2",
                    screen_type="home",
                    screenshot_path="screenshots/screen_1.png",
                    screenshot_status="captured",
                ),
            ],
            screens_discovered=2,
        )

        assert result.screenshots_captured == 2
        assert result.artifacts_degraded is False
        assert result.degraded_reason is None
        assert result.navigation_failures == 0


# ---------------------------------------------------------------------------
# B. dropped navigation attempts are COUNTED, not only logged
# ---------------------------------------------------------------------------


def _clickable(label: str) -> dict:
    return {
        "text": label,
        "content_desc": "",
        "resource_id": "",
        "location": {"x": 0, "y": 0},
        "size": {"width": 10, "height": 10},
    }


class TestNavigationFailuresAreCounted:
    async def test_a_failed_tap_increments_the_source_screens_counter(self) -> None:
        nav = ScreenNavigator()
        client = AsyncMock()
        client.get_page_source = AsyncMock(return_value="<hierarchy/>")
        client.find_clickable_elements = AsyncMock(
            return_value=[_clickable("Broken"), _clickable("AlsoBroken")]
        )
        client.tap = AsyncMock(side_effect=OSError("tap failed"))
        client.back = AsyncMock()

        screens, edges = await nav.discover(client, max_screens=5)

        assert edges == []
        assert len(screens) == 1
        # Both dropped attempts are attributed to the screen they were made FROM.
        assert screens[0].navigation_failures == 2

    async def test_a_clean_crawl_counts_zero(self) -> None:
        nav = ScreenNavigator()
        client = AsyncMock()
        client.get_page_source = AsyncMock(return_value="<hierarchy/>")
        client.find_clickable_elements = AsyncMock(return_value=[])

        screens, _edges = await nav.discover(client, max_screens=5)

        assert screens[0].navigation_failures == 0

    async def test_the_count_survives_serialization_to_the_wire(self) -> None:
        """End to end through the real producer/consumer plumbing: the dropped
        count reaches ``InspectionResultResponse``, so a 0-edge graph is
        distinguishable from an unmeasured one."""
        nav = ScreenNavigator()
        client = AsyncMock()
        client.get_page_source = AsyncMock(return_value="<hierarchy/>")
        client.find_clickable_elements = AsyncMock(return_value=[_clickable("Broken")])
        client.tap = AsyncMock(side_effect=RuntimeError("driver gone"))
        client.back = AsyncMock()

        screens, edges = await nav.discover(client, max_screens=5)
        screens_data = [_serialize_screen(s) for s in screens]
        assert screens_data[0]["navigation_failures"] == 1

        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[_build_screen_info(s) for s in screens_data],
            navigation_graph=[],
            screens_discovered=len(screens),
        )

        assert edges == []
        assert result.navigation_failures == 1

    def test_rows_written_before_the_field_existed_read_as_zero(self) -> None:
        """A legacy row carries no key; that is a floor, not a measurement."""
        legacy = {"screen_id": "s1", "screen_type": "home"}

        assert _build_screen_info(legacy).navigation_failures == 0

    def test_the_aggregate_sums_every_screen_and_cannot_be_overstated(self) -> None:
        result = InspectionResultResponse(
            job_id="j1",
            status="completed",
            screens=[
                ScreenInfo(screen_id="s1", screen_type="home", navigation_failures=3),
                ScreenInfo(screen_id="s2", screen_type="login", navigation_failures=1),
            ],
            navigation_failures=99,
        )

        assert result.navigation_failures == 4
