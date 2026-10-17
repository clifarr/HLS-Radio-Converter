"""TOML reading that works on every Python we support.

``tomllib`` only exists from Python 3.11, so on the Raspberry Pi OS Bullseye
images we still care about (3.9) the import must fall back to ``tomli``. Hiding
that behind one small module keeps the conditional import in exactly one place
instead of duplicating it in every caller.
"""

from __future__ import annotations

import sys
from typing import Any, Dict

if sys.version_info >= (3, 11):
    import tomllib as _impl
else:  # pragma: no cover - exercised on 3.9/3.10, i.e. on the Pi
    import tomli as _impl


def loads(text: str) -> Dict[str, Any]:
    """Parse TOML text into a plain dict."""
    return _impl.loads(text)
