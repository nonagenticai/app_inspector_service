"""Named constants for the app inspector API."""

# Health status strings
HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"

# Job statuses
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_AWAITING_SELECTION = "awaiting_selection"

# Error message truncation
MAX_ERROR_LENGTH = 4000

# Export formats
EXPORT_HTML = "html"
EXPORT_XML = "xml"

# Default max screens to discover
DEFAULT_MAX_SCREENS = 20

# --- Maestro CLI timeouts (seconds) ---
MAESTRO_INSTALL_TIMEOUT = 120
MAESTRO_LAUNCH_TIMEOUT = 30
MAESTRO_HIERARCHY_TIMEOUT = 30
MAESTRO_SCREENSHOT_TIMEOUT = 15
MAESTRO_TAP_TIMEOUT = 10
MAESTRO_BACK_TIMEOUT = 10
MAESTRO_RECORD_TIMEOUT = 10
MAESTRO_DEFAULT_TIMEOUT = 30

# --- Device farm HTTP timeouts (seconds) ---
DEVICE_FARM_LOCAL_HEALTH_TIMEOUT = 5.0
DEVICE_FARM_BROWSERSTACK_CLIENT_TIMEOUT = 30.0
DEVICE_FARM_BROWSERSTACK_UPLOAD_TIMEOUT = 120.0
DEVICE_FARM_BROWSERSTACK_HEALTH_TIMEOUT = 10.0

# --- Device farm artifact (session recording) download limits ---
# Session recordings are streamed to disk-free memory with a hard cap; a body we
# do not control must never be buffered unbounded. 200MB comfortably covers a
# full-length device session recording.
DEVICE_FARM_ARTIFACT_MAX_BYTES = 200 * 1024 * 1024
DEVICE_FARM_ARTIFACT_CHUNK_BYTES = 65536

# --- Auth middleware timeout (seconds) ---
LICENSE_INTROSPECT_TIMEOUT = 2.0

# --- Cleanup service defaults ---
STUCK_JOB_TIMEOUT_MINUTES = 60
RETENTION_DAYS = 30
