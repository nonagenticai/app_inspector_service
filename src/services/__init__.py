"""Re-exports for the app inspector service layer."""

from src.services.app_acquisition import (
    AcquiredApp,
    AppAcquisitionError,
    AppAcquisitionService,
    AppStoreDownloadError,
    ConfinementError,
    DownloadError,
    ValidationError,
    confine_minio_uri,
    resolve_allowed_minio_bucket,
)
from src.services.appium_client import AppiumClient
from src.services.automation_client import AutomationClient
from src.services.device_farm import (
    BrowserStackFarm,
    DeviceFarmClient,
    DeviceSpec,
    DockerAndroidFarm,
    LocalFarm,
    SessionInfo,
    get_device_farm,
)
from src.services.inspector_service import InspectorService
from src.services.job_manager import JobManager
from src.services.maestro_client import MaestroClient
from src.services.screen_classifier import classify_screen
from src.services.screen_navigator import DiscoveredScreen, NavigationEdge, discover_screens
from src.services.storage_service import StorageService
from src.services.xml_parser import ParsedElement, ParsedScreen, parse_xml_source

__all__ = [
    "AcquiredApp",
    "AppAcquisitionError",
    "AppAcquisitionService",
    "AppStoreDownloadError",
    "AppiumClient",
    "AutomationClient",
    "BrowserStackFarm",
    "ConfinementError",
    "DeviceFarmClient",
    "DeviceSpec",
    "DiscoveredScreen",
    "DockerAndroidFarm",
    "DownloadError",
    "InspectorService",
    "JobManager",
    "LocalFarm",
    "MaestroClient",
    "NavigationEdge",
    "ParsedElement",
    "ParsedScreen",
    "SessionInfo",
    "StorageService",
    "ValidationError",
    "classify_screen",
    "confine_minio_uri",
    "discover_screens",
    "get_device_farm",
    "parse_xml_source",
    "resolve_allowed_minio_bucket",
]
