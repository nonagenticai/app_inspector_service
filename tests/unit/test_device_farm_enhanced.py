"""Tests for enhanced device farm features (Session 6)."""

from __future__ import annotations

import asyncio
import json
import socket
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.device_farm import (
    BrowserStackFarm,
    DockerAndroidFarm,
    LocalFarm,
)

# An allowlisted BrowserStack artifact host (see
# ``device_farm.BROWSERSTACK_ARTIFACT_HOST_SUFFIXES``). ``example.com`` no
# longer works: farm-supplied artifact URLs are allowlist-checked.
_VIDEO_URL = "https://automate-sessions.browserstack.com/s3/video.mp4"


def _public_getaddrinfo(_host, *_args, **_kwargs):
    """Resolve every hostname to one PUBLIC IP so the SSRF guard passes."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _run(coro):
    """Helper to run async code in sync tests."""
    return asyncio.run(coro)


class _FakeStreamResponse:
    """Stand-in for the async context manager returned by ``client.stream()``.

    ``BrowserStackFarm._download_artifact`` STREAMS the artifact body against a
    size cap (mirroring ``app_acquisition._download_file``) instead of touching
    ``response.content``, so an artifact mock must expose ``aiter_bytes`` -- a
    plain ``MagicMock`` with ``.content`` set would silently exercise nothing.
    """

    def __init__(self, status_code, body=b"", headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aiter_bytes(self, chunk_size=65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]


# ==========================================================================
# DeviceFarmClient default method tests
# ==========================================================================


class TestDeviceFarmClientDefaults(unittest.TestCase):
    """Test that the ABC default methods work correctly."""

    def test_get_video_returns_none(self):
        farm = LocalFarm()
        result = _run(farm.get_video("some-session"))
        self.assertIsNone(result)

    def test_end_session_is_noop(self):
        farm = LocalFarm()
        # Should not raise
        _run(farm.end_session("some-session"))

    def test_list_devices_returns_empty(self):
        farm = LocalFarm()
        result = _run(farm.list_devices())
        self.assertEqual(result, [])


# ==========================================================================
# DockerAndroidFarm enhanced tests
# ==========================================================================


class TestDockerAndroidFarmIpaRejection(unittest.TestCase):
    """Test that DockerAndroidFarm rejects iOS apps."""

    def test_upload_rejects_ipa(self):
        farm = DockerAndroidFarm()
        with self.assertRaises(ValueError) as ctx:
            _run(farm.upload_app("/path/to/app.ipa"))
        self.assertIn("does not support iOS", str(ctx.exception))

    def test_upload_rejects_dot_app(self):
        farm = DockerAndroidFarm()
        with self.assertRaises(ValueError) as ctx:
            _run(farm.upload_app("/path/to/MyApp.app"))
        self.assertIn("does not support iOS", str(ctx.exception))

    def test_upload_rejects_ipa_uppercase(self):
        farm = DockerAndroidFarm()
        with self.assertRaises(ValueError) as ctx:
            _run(farm.upload_app("/path/to/app.IPA"))
        self.assertIn("does not support iOS", str(ctx.exception))


class TestDockerAndroidFarmListDevices(unittest.TestCase):
    """Test DockerAndroidFarm.list_devices() with mocked subprocess."""

    @patch("asyncio.create_subprocess_exec")
    def test_list_devices_parses_json(self, mock_exec):
        container_data = json.dumps(
            {
                "ID": "abc123",
                "Names": "android-emulator",
                "State": "running",
                "Status": "Up 2 hours",
            }
        )
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(container_data.encode(), b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        farm = DockerAndroidFarm(container_name="android-emulator")
        devices = _run(farm.list_devices())

        self.assertIsInstance(devices, list)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["Names"], "android-emulator")

    @patch("asyncio.create_subprocess_exec")
    def test_list_devices_empty_output(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        farm = DockerAndroidFarm()
        devices = _run(farm.list_devices())
        self.assertEqual(devices, [])

    @patch("asyncio.create_subprocess_exec")
    def test_list_devices_handles_failure(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1
        mock_exec.return_value = mock_proc

        farm = DockerAndroidFarm()
        devices = _run(farm.list_devices())
        self.assertEqual(devices, [])


class TestDockerAndroidFarmGetVideo(unittest.TestCase):
    """Test DockerAndroidFarm.get_video() with mocked subprocess."""

    @patch("asyncio.create_subprocess_exec")
    def test_get_video_returns_bytes(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"fake-video-data", b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        farm = DockerAndroidFarm(container_name="android-emulator")
        video = _run(farm.get_video())

        self.assertEqual(video, b"fake-video-data")

    @patch("asyncio.create_subprocess_exec")
    def test_get_video_returns_none_on_failure(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1
        mock_exec.return_value = mock_proc

        farm = DockerAndroidFarm()
        video = _run(farm.get_video())
        self.assertIsNone(video)


# ==========================================================================
# BrowserStackFarm enhanced tests
# ==========================================================================


class TestBrowserStackFarmGetVideo(unittest.TestCase):
    """Test BrowserStackFarm.get_video() with mocked httpx."""

    @patch("src.helpers.url_validation.socket.getaddrinfo", _public_getaddrinfo)
    @patch("httpx.AsyncClient")
    def test_get_video_returns_bytes(self, mock_client_cls):
        session_response = MagicMock()
        session_response.status_code = 200
        session_response.json.return_value = {"automation_session": {"video_url": _VIDEO_URL}}

        video_response = _FakeStreamResponse(200, b"browserstack-video-data")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=session_response)
        mock_client.stream = MagicMock(return_value=video_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        video = _run(farm.get_video("session-123"))

        self.assertEqual(video, b"browserstack-video-data")

    @patch("httpx.AsyncClient")
    def test_get_video_returns_none_no_url(self, mock_client_cls):
        session_response = MagicMock()
        session_response.status_code = 200
        session_response.json.return_value = {"automation_session": {}}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=session_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        video = _run(farm.get_video("session-123"))
        self.assertIsNone(video)


class TestBrowserStackFarmListDevices(unittest.TestCase):
    """Test BrowserStackFarm.list_devices() with mocked httpx."""

    @patch("httpx.AsyncClient")
    def test_list_devices_returns_list(self, mock_client_cls):
        devices_data = [
            {"device": "Samsung Galaxy S21", "os_version": "11.0"},
            {"device": "Google Pixel 5", "os_version": "12.0"},
        ]
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = devices_data

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        devices = _run(farm.list_devices())

        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]["device"], "Samsung Galaxy S21")

    @patch("httpx.AsyncClient")
    def test_list_devices_handles_error(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("Network error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        devices = _run(farm.list_devices())
        self.assertEqual(devices, [])


class TestBrowserStackFarmEndSession(unittest.TestCase):
    """Test BrowserStackFarm.end_session() with mocked httpx."""

    @patch("httpx.AsyncClient")
    def test_end_session_calls_api(self, mock_client_cls):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.put = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        _run(farm.end_session("session-123"))

        mock_client.put.assert_called_once()
        call_args = mock_client.put.call_args
        self.assertIn("session-123", call_args[0][0])

    @patch("httpx.AsyncClient")
    def test_end_session_handles_error(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.put = AsyncMock(side_effect=OSError("Network error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        # Should not raise
        _run(farm.end_session("session-123"))


# ==========================================================================
# LocalFarm default methods work
# ==========================================================================


class TestLocalFarmDefaults(unittest.TestCase):
    """Verify LocalFarm inherits working default methods."""

    def test_local_farm_get_video(self):
        farm = LocalFarm()
        result = _run(farm.get_video())
        self.assertIsNone(result)

    def test_local_farm_end_session(self):
        farm = LocalFarm()
        _run(farm.end_session())  # Should not raise

    def test_local_farm_list_devices(self):
        farm = LocalFarm()
        result = _run(farm.list_devices())
        self.assertEqual(result, [])

    def test_local_farm_name(self):
        farm = LocalFarm()
        self.assertEqual(farm.name, "local")


# ==========================================================================
# LocalFarm health_check tests (lines 101-106)
# ==========================================================================


class TestLocalFarmHealthCheck(unittest.TestCase):
    """Test LocalFarm.health_check()."""

    @patch("src.services.device_farm.httpx.AsyncClient")
    def test_health_check_success(self, mock_client_cls):
        response = MagicMock()
        response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = LocalFarm()
        result = _run(farm.health_check())
        self.assertTrue(result)

    @patch("src.services.device_farm.httpx.AsyncClient")
    def test_health_check_non_200(self, mock_client_cls):
        response = MagicMock()
        response.status_code = 503

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = LocalFarm()
        result = _run(farm.health_check())
        self.assertFalse(result)

    @patch("src.services.device_farm.httpx.AsyncClient")
    def test_health_check_exception(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = LocalFarm()
        result = _run(farm.health_check())
        self.assertFalse(result)


# ==========================================================================
# LocalFarm get_capabilities tests (lines 87-98)
# ==========================================================================


class TestLocalFarmCapabilities(unittest.TestCase):
    """Test LocalFarm.get_capabilities()."""

    def test_capabilities_no_device(self):
        farm = LocalFarm()
        caps = _run(farm.get_capabilities())
        self.assertEqual(caps["platformName"], "Android")
        self.assertEqual(caps["appium:automationName"], "UiAutomator2")

    def test_capabilities_with_device(self):
        from src.services.device_farm import DeviceSpec

        farm = LocalFarm()
        device = DeviceSpec(name="Pixel 5", os_version="12.0", platform="android")
        caps = _run(farm.get_capabilities(device))
        self.assertEqual(caps["platformName"], "Android")
        self.assertEqual(caps["appium:deviceName"], "Pixel 5")
        self.assertEqual(caps["appium:platformVersion"], "12.0")

    def test_capabilities_with_default_device_name(self):
        from src.services.device_farm import DeviceSpec

        farm = LocalFarm()
        device = DeviceSpec(name="default", os_version="", platform="ios")
        caps = _run(farm.get_capabilities(device))
        self.assertEqual(caps["platformName"], "Ios")
        # "default" device name should NOT be added to caps
        self.assertNotIn("appium:deviceName", caps)
        # No os_version means no platformVersion
        self.assertNotIn("appium:platformVersion", caps)


# ==========================================================================
# LocalFarm upload_app (line 82)
# ==========================================================================


class TestLocalFarmUploadApp(unittest.TestCase):
    def test_upload_returns_path_unchanged(self):
        farm = LocalFarm()
        result = _run(farm.upload_app("/path/to/app.apk"))
        self.assertEqual(result, "/path/to/app.apk")


# ==========================================================================
# LocalFarm get_appium_url (line 85)
# ==========================================================================


class TestLocalFarmGetAppiumUrl(unittest.TestCase):
    def test_default_appium_url(self):
        farm = LocalFarm()
        result = _run(farm.get_appium_url())
        self.assertEqual(result, "http://localhost:4723")

    def test_custom_appium_url(self):
        farm = LocalFarm(appium_url="http://remote:4723")
        result = _run(farm.get_appium_url())
        self.assertEqual(result, "http://remote:4723")


# ==========================================================================
# DockerAndroidFarm upload_app for APK (lines 123-134)
# ==========================================================================


class TestDockerAndroidFarmUploadApk(unittest.TestCase):
    """Test DockerAndroidFarm.upload_app() with a valid APK."""

    @patch("asyncio.create_subprocess_exec")
    def test_upload_apk_success(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        farm = DockerAndroidFarm(container_name="test-emu")
        result = _run(farm.upload_app("/path/to/app.apk"))
        self.assertEqual(result, "/tmp/app.apk")

        # Verify docker cp was called
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        self.assertIn("docker", call_args)
        self.assertIn("cp", call_args)


# ==========================================================================
# DockerAndroidFarm get_appium_url and name (lines 136-140)
# ==========================================================================


class TestDockerAndroidFarmMeta(unittest.TestCase):
    def test_name(self):
        farm = DockerAndroidFarm()
        self.assertEqual(farm.name, "docker_android")

    def test_get_appium_url(self):
        farm = DockerAndroidFarm()
        result = _run(farm.get_appium_url())
        self.assertEqual(result, "http://localhost:4723")

    def test_get_capabilities(self):
        farm = DockerAndroidFarm()
        caps = _run(farm.get_capabilities())
        self.assertEqual(caps, {"platformName": "Android"})


# ==========================================================================
# DockerAndroidFarm health_check (lines 143-156)
# ==========================================================================


class TestDockerAndroidFarmHealthCheck(unittest.TestCase):
    @patch("asyncio.create_subprocess_exec")
    def test_health_check_healthy(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        farm = DockerAndroidFarm(container_name="emu")
        result = _run(farm.health_check())
        self.assertTrue(result)

    @patch("asyncio.create_subprocess_exec")
    def test_health_check_unhealthy(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"No such object"))
        mock_proc.returncode = 1
        mock_exec.return_value = mock_proc

        farm = DockerAndroidFarm()
        result = _run(farm.health_check())
        self.assertFalse(result)

    @patch("asyncio.create_subprocess_exec")
    def test_health_check_exception(self, mock_exec):
        mock_exec.side_effect = OSError("docker not found")

        farm = DockerAndroidFarm()
        result = _run(farm.health_check())
        self.assertFalse(result)


# ==========================================================================
# DockerAndroidFarm get_video exception path (lines 204-206)
# ==========================================================================


class TestDockerAndroidFarmGetVideoException(unittest.TestCase):
    @patch("asyncio.create_subprocess_exec")
    def test_get_video_exception_returns_none(self, mock_exec):
        mock_exec.side_effect = OSError("docker not available")

        farm = DockerAndroidFarm()
        result = _run(farm.get_video())
        self.assertIsNone(result)


# ==========================================================================
# DockerAndroidFarm list_devices exception path (lines 183-185)
# ==========================================================================


class TestDockerAndroidFarmListDevicesException(unittest.TestCase):
    @patch("asyncio.create_subprocess_exec")
    def test_list_devices_exception_returns_empty(self, mock_exec):
        mock_exec.side_effect = OSError("docker not available")

        farm = DockerAndroidFarm()
        result = _run(farm.list_devices())
        self.assertEqual(result, [])


# ==========================================================================
# BrowserStackFarm core methods (lines 228-267)
# ==========================================================================


class TestBrowserStackFarmCoreMethods(unittest.TestCase):
    def test_name(self):
        farm = BrowserStackFarm(username="user", access_key="key")
        self.assertEqual(farm.name, "browserstack")

    def test_get_appium_url(self):
        farm = BrowserStackFarm(username="user", access_key="key")
        url = _run(farm.get_appium_url())
        self.assertIn("user", url)
        self.assertIn("key", url)
        self.assertIn("hub-cloud.browserstack.com", url)

    def test_get_capabilities_no_device(self):
        farm = BrowserStackFarm(username="user", access_key="key")
        caps = _run(farm.get_capabilities())
        self.assertIn("bstack:options", caps)
        self.assertTrue(caps["bstack:options"]["video"])

    def test_get_capabilities_with_device(self):
        from src.services.device_farm import DeviceSpec

        farm = BrowserStackFarm(username="user", access_key="key")
        device = DeviceSpec(name="Samsung Galaxy S21", os_version="11.0", real_device=True)
        caps = _run(farm.get_capabilities(device))
        self.assertEqual(caps["bstack:options"]["deviceName"], "Samsung Galaxy S21")
        self.assertEqual(caps["bstack:options"]["osVersion"], "11.0")
        self.assertTrue(caps["bstack:options"]["realMobile"])

    @patch("src.services.device_farm.httpx.AsyncClient")
    def test_upload_app_success(self, mock_client_cls):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = {"app_url": "bs://abc123"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".apk") as f:
            f.write(b"fake apk content")
            f.flush()

            farm = BrowserStackFarm(username="user", access_key="key")
            result = _run(farm.upload_app(f.name))

        self.assertEqual(result, "bs://abc123")

    @patch("src.services.device_farm.httpx.AsyncClient")
    def test_health_check_success(self, mock_client_cls):
        response = MagicMock()
        response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        result = _run(farm.health_check())
        self.assertTrue(result)

    @patch("src.services.device_farm.httpx.AsyncClient")
    def test_health_check_failure(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("Network error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        result = _run(farm.health_check())
        self.assertFalse(result)


# ==========================================================================
# BrowserStackFarm get_video edge cases (lines 271-293)
# ==========================================================================


class TestBrowserStackFarmGetVideoEdgeCases(unittest.TestCase):
    def test_get_video_no_session_id(self):
        farm = BrowserStackFarm(username="user", access_key="key")
        result = _run(farm.get_video(""))
        self.assertIsNone(result)

    @patch("httpx.AsyncClient")
    def test_get_video_session_api_non_200(self, mock_client_cls):
        response = MagicMock()
        response.status_code = 404

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        result = _run(farm.get_video("session-123"))
        self.assertIsNone(result)

    @patch("src.helpers.url_validation.socket.getaddrinfo", _public_getaddrinfo)
    @patch("httpx.AsyncClient")
    def test_get_video_download_non_200(self, mock_client_cls):
        session_response = MagicMock()
        session_response.status_code = 200
        session_response.json.return_value = {"automation_session": {"video_url": _VIDEO_URL}}

        video_response = _FakeStreamResponse(500)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=session_response)
        mock_client.stream = MagicMock(return_value=video_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        result = _run(farm.get_video("session-123"))
        self.assertIsNone(result)

    @patch("httpx.AsyncClient")
    def test_get_video_exception(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("Network error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        result = _run(farm.get_video("session-123"))
        self.assertIsNone(result)


# ==========================================================================
# BrowserStackFarm list_devices edge cases (lines 295-310)
# ==========================================================================


class TestBrowserStackFarmListDevicesEdgeCases(unittest.TestCase):
    @patch("httpx.AsyncClient")
    def test_list_devices_non_200(self, mock_client_cls):
        response = MagicMock()
        response.status_code = 500

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        farm = BrowserStackFarm(username="user", access_key="key")
        result = _run(farm.list_devices())
        self.assertEqual(result, [])


# ==========================================================================
# BrowserStackFarm end_session edge cases (lines 312-327)
# ==========================================================================


class TestBrowserStackFarmEndSessionEdgeCases(unittest.TestCase):
    def test_end_session_empty_session_id(self):
        farm = BrowserStackFarm(username="user", access_key="key")
        # Should return without doing anything
        _run(farm.end_session(""))


# ==========================================================================
# get_device_farm factory (lines 338-345)
# ==========================================================================


class TestGetDeviceFarmFactory(unittest.TestCase):
    def test_get_local_farm(self):
        from src.services.device_farm import get_device_farm

        farm = get_device_farm("local")
        self.assertIsInstance(farm, LocalFarm)

    def test_get_docker_farm(self):
        from src.services.device_farm import get_device_farm

        farm = get_device_farm("docker_android")
        self.assertIsInstance(farm, DockerAndroidFarm)

    def test_get_browserstack_farm(self):
        from src.services.device_farm import get_device_farm

        farm = get_device_farm("browserstack", username="u", access_key="k")
        self.assertIsInstance(farm, BrowserStackFarm)

    def test_unknown_provider_raises(self):
        from src.services.device_farm import get_device_farm

        with self.assertRaises(ValueError) as ctx:
            get_device_farm("unknown_provider")
        self.assertIn("Unknown device farm", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
