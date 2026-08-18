"""Resolve HQR_HOME for standalone skill scripts.

Skill scripts may run outside the HQ Runtime process (system Python, nix env,
CI) where ``hqr_constants`` is not importable.  This module provides the
same ``get_hqr_home()`` contract without requiring it on ``sys.path``.

When ``hqr_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from hqr_constants import get_hqr_home as get_hqr_home
except (ModuleNotFoundError, ImportError):

    def get_hqr_home() -> Path:
        """Return the HQ Runtime home directory (default: ``~/.hqr``)."""
        val = os.environ.get("HQR_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hqr"
