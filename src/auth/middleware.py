"""License token authentication middleware — delegates to w2v_common.

This module re-exports the shared auth middleware so that existing imports
throughout the service (``from src.auth.middleware import ...``) continue to
work without modification.
"""

from typing import Any

# Re-export the lazy-init cache so tests that do
# ``from src.auth.middleware import _license_cache`` get a live reference
# on first access after introspection has initialised it.
import w2v_common.auth.middleware as _mw_mod


def __getattr__(name: str) -> Any:
    """Delegate attribute lookups to w2v_common.auth.middleware.

    This ensures that ``_license_cache`` reflects runtime state (it starts as
    ``None`` and is lazily initialised) and that ``@patch("src.auth.middleware.httpx...")``
    in tests resolves correctly against the upstream module.
    """
    try:
        return getattr(_mw_mod, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
