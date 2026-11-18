"""Unchecked subprocess results and unbounded waits in the device-farm path.

Three defects, all in the "the call returned, so it worked" family:

#3 ``DockerAndroidFarm.upload_app`` awaited ``proc.wait()`` and returned
   ``/tmp/app.apk`` regardless of the exit code. A failed ``docker cp`` left
   whatever APK the SHARED container already held in place, so Appium inspected
   the PREVIOUS job's app — potentially another tenant's.

#4 ``upload_app`` and ``health_check`` both spawn with ``stdout=PIPE`` and then
   ``await proc.wait()`` without ever draining. Once the child writes past the
   ~64KiB pipe buffer it blocks on write while we block on wait: a permanent
   deadlock with no timeout, hanging ``/api/farms`` and the health check.

#4b ``AppiumClient.connect`` called ``webdriver.Remote`` inside
   ``run_in_executor`` with no client timeout — a black-holed hub hangs the
   worker thread forever.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.services.device_farm import DockerAndroidFarm

# Any test that pins "this is bounded" must itself be bounded, or a regression
# hangs the suite instead of failing it.
_BOUND = 5.0


class _FakeProc:
    """A subprocess double whose ``wait()`` models the real pipe deadlock.

    With ``stdout=PIPE`` and an undrained buffer, ``proc.wait()`` genuinely
    never returns — the child is blocked writing, we are blocked waiting. Only
    ``communicate()`` (which drains) completes. Reproducing that here is what
    makes this test fail against the old code instead of quietly passing.
    """

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.communicate_called = False

    async def wait(self) -> int:
        await asyncio.Event().wait()  # never completes — the deadlock
        raise AssertionError("unreachable")

    async def communicate(self, input=None):  # noqa: A002
        self.communicate_called = True
        return self._stdout, self._stderr


def _spawn(proc: _FakeProc):
    async def _create(*args, **kwargs):
        return proc

    return _create


# ---------------------------------------------------------------------------
# #3 — a failed upload must NOT report success
# ---------------------------------------------------------------------------


def test_failed_docker_cp_raises_instead_of_returning_a_stale_path():
    """The core cross-tenant hazard: exit code 1, yet ``/tmp/app.apk`` returned."""
    farm = DockerAndroidFarm()
    proc = _FakeProc(returncode=1, stderr=b"Error: No such container: android-emulator")

    async def _run():
        with patch("asyncio.create_subprocess_exec", _spawn(proc)):
            return await farm.upload_app("/tmp/app_inspector_test/app.apk")

    with pytest.raises(RuntimeError) as ctx:
        asyncio.run(asyncio.wait_for(_run(), timeout=_BOUND))

    # The operator needs the reason, and the stale path must not be the answer.
    assert "No such container" in str(ctx.value)


def test_successful_docker_cp_still_returns_the_container_path():
    """The guard must not break the happy path."""
    farm = DockerAndroidFarm()
    proc = _FakeProc(returncode=0)

    async def _run():
        with patch("asyncio.create_subprocess_exec", _spawn(proc)):
            return await farm.upload_app("/tmp/app_inspector_test/app.apk")

    assert asyncio.run(asyncio.wait_for(_run(), timeout=_BOUND)) == "/tmp/app.apk"


# ---------------------------------------------------------------------------
# #4 — neither call may block forever on an undrained pipe
# ---------------------------------------------------------------------------


def test_upload_app_drains_the_pipe_instead_of_deadlocking():
    farm = DockerAndroidFarm()
    proc = _FakeProc(returncode=0, stdout=b"x" * 200_000)

    async def _run():
        with patch("asyncio.create_subprocess_exec", _spawn(proc)):
            return await farm.upload_app("/tmp/app_inspector_test/app.apk")

    asyncio.run(asyncio.wait_for(_run(), timeout=_BOUND))
    assert proc.communicate_called, "upload_app must drain stdout, not await wait()"


def test_health_check_drains_the_pipe_instead_of_deadlocking():
    """device_farm.py:327 — ``docker inspect`` with PIPE + ``wait()``."""
    farm = DockerAndroidFarm()
    proc = _FakeProc(returncode=0, stdout=b"x" * 200_000)

    async def _run():
        with patch("asyncio.create_subprocess_exec", _spawn(proc)):
            return await farm.health_check()

    assert asyncio.run(asyncio.wait_for(_run(), timeout=_BOUND)) is True
    assert proc.communicate_called, "health_check must drain stdout, not await wait()"


def test_health_check_reports_false_when_the_container_is_absent():
    farm = DockerAndroidFarm()
    proc = _FakeProc(returncode=1, stderr=b"No such object")

    async def _run():
        with patch("asyncio.create_subprocess_exec", _spawn(proc)):
            return await farm.health_check()

    assert asyncio.run(asyncio.wait_for(_run(), timeout=_BOUND)) is False


class _HangingProc(_FakeProc):
    """Even a DRAINING call must not wait forever — docker itself can wedge."""

    def __init__(self) -> None:
        super().__init__()
        self.killed = False

    async def communicate(self, input=None):  # noqa: A002
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def kill(self) -> None:
        self.killed = True


def test_health_check_gives_up_on_a_wedged_docker_cli():
    """A hung ``docker inspect`` must degrade to False, not hang /api/farms."""
    farm = DockerAndroidFarm()
    proc = _HangingProc()

    async def _run():
        with (
            patch("asyncio.create_subprocess_exec", _spawn(proc)),
            patch.object(farm, "_subprocess_timeout", 0.1, create=True),
        ):
            return await farm.health_check()

    assert asyncio.run(asyncio.wait_for(_run(), timeout=_BOUND)) is False
    assert proc.killed, "a timed-out subprocess must be killed, not leaked"


# ---------------------------------------------------------------------------
# #4b — the Appium connect must carry a client timeout
# ---------------------------------------------------------------------------


def test_appium_connect_passes_a_bounded_client_timeout():
    """appium_client.py:68 — ``webdriver.Remote`` with no client timeout.

    Pins the PROPERTY (a finite timeout reaches Selenium's HTTP layer), read off
    whatever ``webdriver.Remote`` is actually called with in production — not a
    helper in isolation.
    """
    from unittest.mock import AsyncMock

    from src.services.appium_client import AppiumClient

    client = AppiumClient(appium_url="http://localhost:4723", platform="android")

    mock_driver = MagicMock()
    mock_driver.session_id = "sess-1"

    async def _run():
        with (
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
            return loop.run_in_executor.await_args

    call = asyncio.run(asyncio.wait_for(_run(), timeout=_BOUND))
    bound = call.args[1]  # the functools.partial handed to the executor

    client_config = bound.keywords.get("client_config")
    assert client_config is not None, "webdriver.Remote must receive a client_config"

    timeout = getattr(client_config, "timeout", None)
    assert isinstance(timeout, int | float) and 0 < timeout < 3600, (
        f"webdriver.Remote must receive a finite client timeout, got {timeout!r}"
    )
