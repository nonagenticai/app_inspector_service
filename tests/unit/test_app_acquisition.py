"""Unit tests for app acquisition service and URL validation."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.helpers.url_validation import SSRFError, validate_url_safe
from src.services.app_acquisition import (
    AcquiredApp,
    AppAcquisitionError,
    AppAcquisitionService,
    AppStoreDownloadError,
    ConfinementError,
    ValidationError,
    _detect_platform_from_ext,
    confine_minio_uri,
    resolve_allowed_minio_bucket,
)

# The one bucket minio:// app_paths may name in these tests.
_BUCKET = "w2v-app-inspector"
_TENANT = "e148e8af-0000-4000-8000-000000000001"
_OTHER_TENANT = "ffffffff-0000-4000-8000-000000000002"


def _settings_mock(*, allowed_minio_bucket: str = _BUCKET) -> MagicMock:
    """Settings double carrying REAL strings for the confinement fields."""
    return MagicMock(
        acquisition=MagicMock(
            download_timeout=300,
            max_file_size_mb=500,
            temp_dir="/tmp/test",
            allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            allowed_minio_bucket=allowed_minio_bucket,
        ),
        storage=MagicMock(minio_bucket=_BUCKET),
    )


# ===========================================================================
# Helper to create a fake APK file with PK magic bytes
# ===========================================================================


def _create_fake_apk(path: Path) -> None:
    """Write PK magic bytes to simulate a valid APK."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"PK\x03\x04" + b"\x00" * 100)


def _create_fake_ipa(path: Path) -> None:
    """Write PK magic bytes to simulate a valid IPA."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"PK\x03\x04" + b"\x00" * 100)


# ===========================================================================
# URL Validation (SSRF protection) tests
# ===========================================================================


class TestURLValidation(unittest.TestCase):
    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_public_url_passes(self, mock_dns):
        """Public URLs should pass validation without error.

        DNS is mocked: validation is now fail-CLOSED, so a sandboxed runner with
        no resolver would otherwise turn this into a hard failure.
        """
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 0))]
        try:
            validate_url_safe("https://example.com/app.apk")
        except SSRFError:
            self.fail("Public URL should not be blocked")

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_localhost_blocked(self, mock_dns):
        mock_dns.return_value = [
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]
        with self.assertRaises(SSRFError) as ctx:
            validate_url_safe("http://localhost/app.apk")
        self.assertIn("127.0.0.1", str(ctx.exception))

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_private_ip_blocked(self, mock_dns):
        mock_dns.return_value = [
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]
        with self.assertRaises(SSRFError):
            validate_url_safe("http://internal-server/app.apk")

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_cgnat_blocked(self, mock_dns):
        mock_dns.return_value = [
            (2, 1, 6, "", ("100.64.0.1", 0)),
        ]
        with self.assertRaises(SSRFError):
            validate_url_safe("http://cgnat-host/app.apk")

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_link_local_blocked(self, mock_dns):
        mock_dns.return_value = [
            (2, 1, 6, "", ("169.254.1.1", 0)),
        ]
        with self.assertRaises(SSRFError):
            validate_url_safe("http://link-local-host/app.apk")

    def test_metadata_hostname_blocked(self):
        with self.assertRaises(SSRFError) as ctx:
            validate_url_safe("http://metadata.google.internal/computeMetadata/v1/")
        self.assertIn("cloud metadata", str(ctx.exception))

    def test_no_hostname_raises(self):
        with self.assertRaises(ValueError):
            validate_url_safe("not-a-url")

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    def test_public_ip_allowed(self, mock_dns):
        mock_dns.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]
        # Should not raise
        validate_url_safe("https://example.com/app.apk")


# ===========================================================================
# Platform detection tests
# ===========================================================================


class TestPlatformDetection(unittest.TestCase):
    def test_apk_is_android(self):
        self.assertEqual(_detect_platform_from_ext(".apk"), "android")

    def test_aab_is_android(self):
        self.assertEqual(_detect_platform_from_ext(".aab"), "android")

    def test_xapk_is_android(self):
        self.assertEqual(_detect_platform_from_ext(".xapk"), "android")

    def test_ipa_is_ios(self):
        self.assertEqual(_detect_platform_from_ext(".ipa"), "ios")

    def test_app_is_ios(self):
        self.assertEqual(_detect_platform_from_ext(".app"), "ios")


# ===========================================================================
# AppAcquisitionService tests
# ===========================================================================


class TestAppAcquisitionLocalPath(unittest.TestCase):
    """Test local file path acquisition."""

    @patch("src.services.app_acquisition.get_settings")
    def test_valid_apk_local_path(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                acquisition=MagicMock(
                    download_timeout=300,
                    max_file_size_mb=500,
                    temp_dir="/tmp/test",
                    allowed_local_paths=[tmpdir],
                )
            )
            apk_path = Path(tmpdir) / "test.apk"
            _create_fake_apk(apk_path)

            svc = AppAcquisitionService()
            result = asyncio.run(svc.acquire(str(apk_path)))

            self.assertIsInstance(result, AcquiredApp)
            self.assertEqual(result.local_path, apk_path.resolve())
            self.assertEqual(result.platform, "android")
            self.assertEqual(result.original_input, str(apk_path))

    @patch("src.services.app_acquisition.get_settings")
    def test_valid_ipa_local_path(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                acquisition=MagicMock(
                    download_timeout=300,
                    max_file_size_mb=500,
                    temp_dir="/tmp/test",
                    allowed_local_paths=[tmpdir],
                )
            )
            ipa_path = Path(tmpdir) / "test.ipa"
            _create_fake_ipa(ipa_path)

            svc = AppAcquisitionService()
            result = asyncio.run(svc.acquire(str(ipa_path)))

            self.assertEqual(result.platform, "ios")

    @patch("src.services.app_acquisition.get_settings")
    def test_missing_local_file_raises(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/nonexistent"],
            )
        )
        svc = AppAcquisitionService()

        with self.assertRaises(AppAcquisitionError) as ctx:
            asyncio.run(svc.acquire("/nonexistent/path/app.apk"))
        self.assertIn("not found", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_invalid_extension_raises(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                acquisition=MagicMock(
                    download_timeout=300,
                    max_file_size_mb=500,
                    temp_dir="/tmp/test",
                    allowed_local_paths=[tmpdir],
                )
            )
            txt_path = Path(tmpdir) / "readme.txt"
            txt_path.write_text("not an app")

            svc = AppAcquisitionService()

            with self.assertRaises(ValidationError):
                asyncio.run(svc.acquire(str(txt_path)))

    @patch("src.services.app_acquisition.get_settings")
    def test_path_traversal_rejected(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with self.assertRaises(AppAcquisitionError) as ctx:
            asyncio.run(svc.acquire("../../etc/passwd"))
        self.assertIn("not within allowed directories", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_path_traversal_with_dotdot_in_allowed_base(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.return_value = MagicMock(
                acquisition=MagicMock(
                    download_timeout=300,
                    max_file_size_mb=500,
                    temp_dir="/tmp/test",
                    allowed_local_paths=[tmpdir],
                )
            )
            # Create a file outside the allowed dir
            svc = AppAcquisitionService()

            with self.assertRaises(AppAcquisitionError) as ctx:
                asyncio.run(svc.acquire(f"{tmpdir}/../../../etc/passwd"))
            self.assertIn("not within allowed directories", str(ctx.exception))


class TestAppAcquisitionEmpty(unittest.TestCase):
    @patch("src.services.app_acquisition.get_settings")
    def test_empty_input_raises(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with self.assertRaises(AppAcquisitionError) as ctx:
            asyncio.run(svc.acquire(""))
        self.assertIn("empty", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_whitespace_only_raises(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with self.assertRaises(AppAcquisitionError):
            asyncio.run(svc.acquire("   "))


class TestAppAcquisitionPlayStore(unittest.TestCase):
    @patch("src.services.app_acquisition.get_settings")
    def test_play_store_url_detected(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with patch.object(svc, "_from_play_store", new_callable=AsyncMock) as mock_play:
            mock_play.return_value = AcquiredApp(
                local_path=Path("/tmp/test.apk"),
                platform="android",
                original_input="https://play.google.com/store/apps/details?id=com.example.app",
            )
            result = asyncio.run(
                svc.acquire("https://play.google.com/store/apps/details?id=com.example.app")
            )
            mock_play.assert_called_once()
            self.assertEqual(result.platform, "android")


class TestAppAcquisitionAppStore(unittest.TestCase):
    @patch("src.services.app_acquisition.get_settings")
    def test_app_store_url_rejected(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with self.assertRaises(AppStoreDownloadError) as ctx:
            asyncio.run(svc.acquire("https://apps.apple.com/us/app/example/id123456789"))
        self.assertIn("not supported", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_itunes_url_rejected(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with self.assertRaises(AppStoreDownloadError):
            asyncio.run(svc.acquire("https://itunes.apple.com/us/app/example/id123456789"))


class TestAppAcquisitionHTTPURL(unittest.TestCase):
    @patch("src.services.app_acquisition.get_settings")
    def test_http_url_dispatches_to_from_url(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with patch.object(svc, "_from_url", new_callable=AsyncMock) as mock_url:
            mock_url.return_value = AcquiredApp(
                local_path=Path("/tmp/downloaded.apk"),
                platform="android",
                original_input="https://example.com/app.apk",
            )
            result = asyncio.run(svc.acquire("https://example.com/app.apk"))
            mock_url.assert_called_once_with("https://example.com/app.apk")
            self.assertEqual(result.platform, "android")

    @patch("src.services.app_acquisition.validate_url_safe")
    @patch("src.services.app_acquisition.get_settings")
    def test_ssrf_url_rejected(self, mock_settings, mock_validate):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        mock_validate.side_effect = SSRFError("Blocked address 127.0.0.1")

        svc = AppAcquisitionService()

        with self.assertRaises(SSRFError):
            asyncio.run(svc.acquire("http://localhost/evil.apk"))


class TestAppAcquisitionMinIO(unittest.TestCase):
    @patch("src.services.app_acquisition.get_settings")
    def test_minio_uri_dispatches_with_tenant(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        svc = AppAcquisitionService()
        uri = f"minio://{_BUCKET}/{_TENANT}/path/app.apk"

        with patch.object(svc, "_from_minio", new_callable=AsyncMock) as mock_minio:
            mock_minio.return_value = AcquiredApp(
                local_path=Path("/tmp/minio_app.apk"),
                platform="android",
                original_input=uri,
            )
            result = asyncio.run(svc.acquire(uri, tenant_id=_TENANT))
            mock_minio.assert_called_once_with(uri, tenant_id=_TENANT)
            self.assertEqual(result.platform, "android")

    @patch("src.services.app_acquisition.get_settings")
    def test_minio_without_storage_raises(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        svc = AppAcquisitionService(minio_storage=None)

        with self.assertRaises(AppAcquisitionError) as ctx:
            asyncio.run(svc.acquire(f"minio://{_BUCKET}/{_TENANT}/app.apk", tenant_id=_TENANT))
        self.assertIn("not configured", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_unsupported_scheme_rejected(self, mock_settings):
        """s3:// / file:// must NOT fall through to the local-path branch."""
        mock_settings.return_value = _settings_mock()
        svc = AppAcquisitionService(minio_storage=AsyncMock())

        for uri in (f"s3://{_BUCKET}/{_TENANT}/app.apk", "file:///etc/passwd"):
            with self.subTest(uri=uri):
                with self.assertRaises(AppAcquisitionError) as ctx:
                    asyncio.run(svc.acquire(uri, tenant_id=_TENANT))
                self.assertIn("Unsupported URI scheme", str(ctx.exception))


# ===========================================================================
# AcquiredApp dataclass tests
# ===========================================================================


class TestAcquiredApp(unittest.TestCase):
    def test_default_farm_url(self):
        app = AcquiredApp(
            local_path=Path("/tmp/test.apk"),
            platform="android",
            original_input="/tmp/test.apk",
        )
        self.assertEqual(app.farm_app_url, "")

    def test_custom_farm_url(self):
        app = AcquiredApp(
            local_path=Path("/tmp/test.apk"),
            platform="android",
            original_input="/tmp/test.apk",
            farm_app_url="https://farm.example.com/uploads/test.apk",
        )
        self.assertEqual(app.farm_app_url, "https://farm.example.com/uploads/test.apk")


# ===========================================================================
# _from_play_store tests (lines 148-157)
# ===========================================================================


class TestFromPlayStore(unittest.TestCase):
    """Test the actual _from_play_store method end-to-end with mocked download."""

    @patch("src.services.app_acquisition.get_settings")
    def test_from_play_store_downloads_and_validates(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with (
            patch.object(
                svc,
                "_download_file",
                new_callable=AsyncMock,
                return_value=Path("/tmp/test.apk"),
            ) as mock_dl,
            patch.object(svc, "_validate_magic_bytes") as mock_validate,
            patch("src.services.app_acquisition.validate_url_safe") as mock_url_safe,
        ):
            result = asyncio.run(
                svc._from_play_store(
                    "https://play.google.com/store/apps/details?id=com.example.app",
                    "com.example.app",
                )
            )

            mock_url_safe.assert_called_once()
            mock_dl.assert_awaited_once()
            mock_validate.assert_called_once_with(Path("/tmp/test.apk"))
            self.assertEqual(result.platform, "android")
            self.assertEqual(
                result.original_input,
                "https://play.google.com/store/apps/details?id=com.example.app",
            )


# ===========================================================================
# _from_minio tests (lines 171-202)
# ===========================================================================


class TestFromMinio(unittest.TestCase):
    """Test _from_minio with mocked MinIO storage."""

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_success(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        mock_minio = AsyncMock()
        mock_minio.download_file = AsyncMock()

        svc = AppAcquisitionService(minio_storage=mock_minio)
        uri = f"minio://{_BUCKET}/{_TENANT}/uploads/app.apk"

        with patch.object(svc, "_validate_magic_bytes"):
            result = asyncio.run(svc._from_minio(uri, tenant_id=_TENANT))

        self.assertEqual(result.platform, "android")
        self.assertEqual(result.original_input, uri)
        mock_minio.download_file.assert_awaited_once()
        await_args = mock_minio.download_file.await_args
        assert await_args is not None  # narrows `_Call | None` for ty
        bucket, key = await_args.args[:2]
        self.assertEqual(bucket, _BUCKET)
        self.assertEqual(key, f"{_TENANT}/uploads/app.apk")

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_invalid_uri_no_bucket(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        mock_minio = AsyncMock()
        svc = AppAcquisitionService(minio_storage=mock_minio)

        with self.assertRaises(AppAcquisitionError) as ctx:
            asyncio.run(svc._from_minio("minio:///path/to/app.apk", tenant_id=_TENANT))
        self.assertIn("Invalid MinIO URI", str(ctx.exception))
        mock_minio.download_file.assert_not_awaited()

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_invalid_extension(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        mock_minio = AsyncMock()
        svc = AppAcquisitionService(minio_storage=mock_minio)

        with self.assertRaises(ValidationError) as ctx:
            asyncio.run(
                svc._from_minio(f"minio://{_BUCKET}/{_TENANT}/readme.txt", tenant_id=_TENANT)
            )
        self.assertIn("Unsupported file extension", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_download_failure(self, mock_settings):
        from src.services.app_acquisition import DownloadError

        mock_settings.return_value = _settings_mock()
        mock_minio = AsyncMock()
        mock_minio.download_file = AsyncMock(side_effect=OSError("S3 failure"))

        svc = AppAcquisitionService(minio_storage=mock_minio)

        with self.assertRaises(DownloadError) as ctx:
            asyncio.run(svc._from_minio(f"minio://{_BUCKET}/{_TENANT}/app.apk", tenant_id=_TENANT))
        self.assertIn("Failed to download from MinIO", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_ios_app(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        mock_minio = AsyncMock()
        mock_minio.download_file = AsyncMock()

        svc = AppAcquisitionService(minio_storage=mock_minio)

        with patch.object(svc, "_validate_magic_bytes"):
            result = asyncio.run(
                svc._from_minio(f"minio://{_BUCKET}/{_TENANT}/path/to/app.ipa", tenant_id=_TENANT)
            )

        self.assertEqual(result.platform, "ios")

    # -- confinement (the whole point of this guard) ------------------------

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_rejects_foreign_bucket(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        mock_minio = AsyncMock()
        svc = AppAcquisitionService(minio_storage=mock_minio)

        with self.assertRaises(ConfinementError) as ctx:
            asyncio.run(
                svc._from_minio(f"minio://w2v-audiobooks/{_TENANT}/app.apk", tenant_id=_TENANT)
            )
        self.assertIn("not permitted", str(ctx.exception))
        mock_minio.download_file.assert_not_awaited()

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_rejects_foreign_tenant_prefix(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        mock_minio = AsyncMock()
        svc = AppAcquisitionService(minio_storage=mock_minio)

        with self.assertRaises(ConfinementError):
            asyncio.run(
                svc._from_minio(f"minio://{_BUCKET}/{_OTHER_TENANT}/app.apk", tenant_id=_TENANT)
            )
        mock_minio.download_file.assert_not_awaited()

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_requires_tenant(self, mock_settings):
        mock_settings.return_value = _settings_mock()
        mock_minio = AsyncMock()
        svc = AppAcquisitionService(minio_storage=mock_minio)

        with self.assertRaises(ConfinementError):
            asyncio.run(svc._from_minio(f"minio://{_BUCKET}/{_TENANT}/app.apk", tenant_id=None))
        mock_minio.download_file.assert_not_awaited()

    @patch("src.services.app_acquisition.get_settings")
    def test_from_minio_unconfigured_bucket_is_fail_closed(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data"],
                allowed_minio_bucket="",
            ),
            storage=MagicMock(minio_bucket=""),
        )
        svc = AppAcquisitionService(minio_storage=AsyncMock())

        with self.assertRaises(ConfinementError) as ctx:
            asyncio.run(svc._from_minio(f"minio://{_BUCKET}/{_TENANT}/app.apk", tenant_id=_TENANT))
        self.assertIn("not configured", str(ctx.exception))


class TestConfineMinioUri(unittest.TestCase):
    """Direct tests of the confinement rule shared by route + service."""

    def _confine(self, uri: str, tenant: str = _TENANT) -> tuple[str, str]:
        return confine_minio_uri(uri, tenant_id=tenant, allowed_bucket=_BUCKET)

    def test_own_tenant_path_allowed_and_normalized(self):
        bucket, key = self._confine(f"minio://{_BUCKET}/{_TENANT}/uploads/./app.apk")
        self.assertEqual(bucket, _BUCKET)
        self.assertEqual(key, f"{_TENANT}/uploads/app.apk")

    def test_percent_in_legitimate_key_is_not_rewritten(self):
        """Confine on the DECODED form, return the RAW form.

        ``%`` is legal in a MinIO object key, so ``My%20App.apk`` and
        ``My App.apk`` are different objects. Kills the mutation
        ``return bucket, posixpath.normpath(decoded_key)`` — which silently
        rewrites the identifier and fetches the wrong object (or 404s).
        """
        for key in (f"{_TENANT}/My%20App.apk", f"{_TENANT}/100%25/app.apk"):
            with self.subTest(key=key):
                _bucket, out = self._confine(f"minio://{_BUCKET}/{key}")
                self.assertEqual(out, key)

    def test_foreign_bucket_rejected(self):
        with self.assertRaises(ConfinementError):
            self._confine(f"minio://some-other-bucket/{_TENANT}/app.apk")

    def test_foreign_tenant_prefix_rejected(self):
        with self.assertRaises(ConfinementError):
            self._confine(f"minio://{_BUCKET}/{_OTHER_TENANT}/app.apk")

    def test_sibling_prefix_rejected(self):
        """``<tenant>evil/`` must not pass a ``<tenant>`` test (segments, not strings)."""
        with self.assertRaises(ConfinementError):
            self._confine(f"minio://{_BUCKET}/{_TENANT}evil/app.apk")

    def test_dotdot_escape_rejected(self):
        for key in (
            f"{_TENANT}/../{_OTHER_TENANT}/app.apk",
            f"{_TENANT}/sub/../../{_OTHER_TENANT}/app.apk",
            "../app.apk",
        ):
            with self.subTest(key=key), self.assertRaises(ConfinementError):
                self._confine(f"minio://{_BUCKET}/{key}")

    def test_percent_encoded_escape_rejected(self):
        for key in (
            f"{_TENANT}/%2e%2e/{_OTHER_TENANT}/app.apk",
            f"{_TENANT}%2f..%2f{_OTHER_TENANT}/app.apk",
            f"{_TENANT}/%252e%252e%252f{_OTHER_TENANT}/app.apk",
        ):
            with self.subTest(key=key), self.assertRaises(ConfinementError):
                self._confine(f"minio://{_BUCKET}/{key}")

    def test_absolute_key_stays_confined(self):
        with self.assertRaises(ConfinementError):
            self._confine(f"minio://{_BUCKET}//{_OTHER_TENANT}/app.apk")

    def test_missing_tenant_rejected(self):
        for tenant in (None, "", "   "):
            with self.subTest(tenant=tenant), self.assertRaises(ConfinementError):
                confine_minio_uri(
                    f"minio://{_BUCKET}/x/app.apk",
                    tenant_id=tenant,
                    allowed_bucket=_BUCKET,
                )

    def test_unsafe_tenant_identifier_rejected(self):
        for tenant in ("../other", "a/b", "%2e%2e"):
            with self.subTest(tenant=tenant), self.assertRaises(ConfinementError):
                self._confine(f"minio://{_BUCKET}/{tenant}/app.apk", tenant=tenant)

    def test_non_minio_scheme_rejected(self):
        with self.assertRaises(ConfinementError):
            self._confine(f"s3://{_BUCKET}/{_TENANT}/app.apk")

    def test_resolve_allowed_bucket_prefers_explicit_then_storage(self):
        explicit = MagicMock(
            acquisition=MagicMock(allowed_minio_bucket="uploads-bucket"),
            storage=MagicMock(minio_bucket=_BUCKET),
        )
        self.assertEqual(resolve_allowed_minio_bucket(explicit), "uploads-bucket")

        fallback = MagicMock(
            acquisition=MagicMock(allowed_minio_bucket=""),
            storage=MagicMock(minio_bucket=_BUCKET),
        )
        self.assertEqual(resolve_allowed_minio_bucket(fallback), _BUCKET)

        unset = MagicMock(
            acquisition=MagicMock(allowed_minio_bucket=""),
            storage=MagicMock(minio_bucket=""),
        )
        self.assertEqual(resolve_allowed_minio_bucket(unset), "")


# ===========================================================================
# _from_url tests (lines 213-227)
# ===========================================================================


class TestFromUrl(unittest.TestCase):
    """Test _from_url with mocked download."""

    @patch("src.services.app_acquisition.get_settings")
    def test_from_url_success_apk(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with (
            patch("src.services.app_acquisition.validate_url_safe"),
            patch.object(
                svc,
                "_download_file",
                new_callable=AsyncMock,
                return_value=Path("/tmp/downloaded.apk"),
            ),
            patch.object(svc, "_validate_magic_bytes"),
        ):
            result = asyncio.run(svc._from_url("https://example.com/releases/app.apk"))

        self.assertEqual(result.platform, "android")
        self.assertEqual(result.original_input, "https://example.com/releases/app.apk")

    @patch("src.services.app_acquisition.get_settings")
    def test_from_url_success_ipa(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with (
            patch("src.services.app_acquisition.validate_url_safe"),
            patch.object(
                svc,
                "_download_file",
                new_callable=AsyncMock,
                return_value=Path("/tmp/downloaded.ipa"),
            ),
            patch.object(svc, "_validate_magic_bytes"),
        ):
            result = asyncio.run(svc._from_url("https://example.com/releases/app.ipa"))

        self.assertEqual(result.platform, "ios")

    @patch("src.services.app_acquisition.get_settings")
    def test_from_url_no_extension_defaults_to_apk(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with (
            patch("src.services.app_acquisition.validate_url_safe"),
            patch.object(
                svc,
                "_download_file",
                new_callable=AsyncMock,
                return_value=Path("/tmp/downloaded.apk"),
            ),
            patch.object(svc, "_validate_magic_bytes"),
        ):
            result = asyncio.run(svc._from_url("https://example.com/download"))

        self.assertEqual(result.platform, "android")

    @patch("src.services.app_acquisition.get_settings")
    def test_from_url_invalid_extension(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with patch("src.services.app_acquisition.validate_url_safe"):
            with self.assertRaises(ValidationError) as ctx:
                asyncio.run(svc._from_url("https://example.com/file.zip"))
            self.assertIn("Unsupported file extension", str(ctx.exception))


# ===========================================================================
# _download_file tests (lines 261-303)
# ===========================================================================


class TestDownloadFile(unittest.TestCase):
    """Test _download_file with mocked httpx."""

    @patch("src.services.app_acquisition.get_settings")
    def test_download_success(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test_dl",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}

        async def fake_aiter_bytes(chunk_size=8192):
            yield b"PK\x03\x04" + b"\x00" * 50

        mock_response.aiter_bytes = fake_aiter_bytes

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncMock())
        mock_client.stream.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        mock_client.stream.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.services.app_acquisition.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(svc._download_file("https://example.com/app.apk"))

        self.assertTrue(result.exists())
        result.unlink(missing_ok=True)

    @patch("src.helpers.url_validation.socket.getaddrinfo")
    @patch("src.services.app_acquisition.get_settings")
    def test_download_redirect_to_private_ip_blocked(self, mock_settings, mock_dns):
        """A 30x redirect to an internal address must be rejected (SSRF guard)."""
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test_dl",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        # Redirect target resolves to a link-local metadata address.
        mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 0))]

        redirect_response = MagicMock()
        redirect_response.status_code = 302
        redirect_response.headers = {"location": "http://metadata.attacker.test/app.apk"}

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncMock())
        mock_client.stream.return_value.__aenter__ = AsyncMock(return_value=redirect_response)
        mock_client.stream.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.services.app_acquisition.httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(SSRFError):
                asyncio.run(svc._download_file("https://example.com/app.apk"))

    @patch("src.services.app_acquisition.get_settings")
    def test_download_content_length_too_large(self, mock_settings):
        from src.services.app_acquisition import DownloadError

        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=1,  # 1 MB max
                temp_dir="/tmp/test_dl",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {"content-length": str(100 * 1024 * 1024)}  # 100 MB

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncMock())
        mock_client.stream.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        mock_client.stream.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.services.app_acquisition.httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(DownloadError) as ctx:
                asyncio.run(svc._download_file("https://example.com/huge.apk"))
            self.assertIn("File too large", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_download_streaming_exceeds_max_size(self, mock_settings):
        from src.services.app_acquisition import DownloadError

        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=1,  # 1 MB max = 1048576 bytes
                temp_dir="/tmp/test_dl",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}  # No content-length

        async def fake_aiter_bytes(chunk_size=8192):
            # Yield chunks that exceed 1 MB
            for _ in range(200):
                yield b"\x00" * 8192

        mock_response.aiter_bytes = fake_aiter_bytes

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncMock())
        mock_client.stream.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        mock_client.stream.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.services.app_acquisition.httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(DownloadError) as ctx:
                asyncio.run(svc._download_file("https://example.com/huge.apk"))
            self.assertIn("exceeded max size", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_download_http_status_error(self, mock_settings):
        import httpx

        from src.services.app_acquisition import DownloadError

        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test_dl",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        mock_response = MagicMock()
        mock_response.status_code = 404
        exc = httpx.HTTPStatusError("Not found", request=MagicMock(), response=mock_response)

        mock_stream_response = AsyncMock()
        mock_stream_response.raise_for_status = MagicMock(side_effect=exc)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncMock())
        mock_client.stream.return_value.__aenter__ = AsyncMock(return_value=mock_stream_response)
        mock_client.stream.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.services.app_acquisition.httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(DownloadError) as ctx:
                asyncio.run(svc._download_file("https://example.com/missing.apk"))
            self.assertIn("HTTP 404", str(ctx.exception))

    @patch("src.services.app_acquisition.get_settings")
    def test_download_generic_http_error(self, mock_settings):
        import httpx

        from src.services.app_acquisition import DownloadError

        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test_dl",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncMock())
        mock_client.stream.return_value.__aenter__ = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        mock_client.stream.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.services.app_acquisition.httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(DownloadError) as ctx:
                asyncio.run(svc._download_file("https://example.com/app.apk"))
            self.assertIn("Failed to download", str(ctx.exception))


# ===========================================================================
# _validate_magic_bytes tests (lines 305-315)
# ===========================================================================


class TestValidateMagicBytes(unittest.TestCase):
    """Test _validate_magic_bytes."""

    @patch("src.services.app_acquisition.get_settings")
    def test_valid_magic_bytes(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as f:
            f.write(b"PK\x03\x04" + b"\x00" * 100)
            tmp_path = Path(f.name)

        try:
            svc._validate_magic_bytes(tmp_path)  # Should not raise
        finally:
            tmp_path.unlink(missing_ok=True)

    @patch("src.services.app_acquisition.get_settings")
    def test_invalid_magic_bytes(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as f:
            f.write(b"NOT_ZIP_CONTENT")
            tmp_path = Path(f.name)

        with self.assertRaises(ValidationError) as ctx:
            svc._validate_magic_bytes(tmp_path)
        self.assertIn("Invalid file", str(ctx.exception))
        # File should be cleaned up
        self.assertFalse(tmp_path.exists())

    @patch("src.services.app_acquisition.get_settings")
    def test_unreadable_file(self, mock_settings):
        mock_settings.return_value = MagicMock(
            acquisition=MagicMock(
                download_timeout=300,
                max_file_size_mb=500,
                temp_dir="/tmp/test",
                allowed_local_paths=["/app/data", "/tmp/app_inspector"],
            )
        )
        svc = AppAcquisitionService()

        with self.assertRaises(ValidationError) as ctx:
            svc._validate_magic_bytes(Path("/nonexistent/file.apk"))
        self.assertIn("Cannot read file", str(ctx.exception))


class TestPathBoundaryAttack(unittest.TestCase):
    """Regression: startswith() allowed /app/data_evil to match /app/data."""

    @patch("src.services.app_acquisition.get_settings")
    def test_path_boundary_attack_rejected(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create allowed base and evil sibling
            allowed_base = Path(tmpdir) / "data"
            allowed_base.mkdir()
            evil_dir = Path(tmpdir) / "data_evil"
            evil_dir.mkdir()
            payload = evil_dir / "payload.apk"
            _create_fake_apk(payload)

            mock_settings.return_value = MagicMock(
                acquisition=MagicMock(
                    download_timeout=300,
                    max_file_size_mb=500,
                    temp_dir="/tmp/test",
                    allowed_local_paths=[str(allowed_base)],
                )
            )
            svc = AppAcquisitionService()

            with self.assertRaises(AppAcquisitionError) as ctx:
                asyncio.run(svc.acquire(str(payload)))
            self.assertIn("not within allowed directories", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
