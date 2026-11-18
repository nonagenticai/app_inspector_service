"""Database connection module — delegates to w2v_common.

This module re-exports the shared database connection utilities so that
existing imports (``from src.database.connection import ...``) continue to
work without modification.
"""

from w2v_common.database.connection import (
    _ensure_database_exists as _ensure_database_exists,
)
from w2v_common.database.connection import (
    close_engine as close_engine,
)
from w2v_common.database.connection import (
    create_engine_with_retry as create_engine_with_retry,
)
from w2v_common.database.connection import (
    get_engine as get_engine,
)
from w2v_common.database.connection import (
    health_check as health_check,
)

__all__ = [
    "_ensure_database_exists",
    "close_engine",
    "create_engine_with_retry",
    "get_engine",
    "health_check",
]
