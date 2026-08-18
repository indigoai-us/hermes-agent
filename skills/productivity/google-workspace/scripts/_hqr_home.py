"""Resolve HQR_HOME for standalone skill scripts.

Skill scripts may run outside the HQ Runtime process (e.g. system Python,
nix env, CI) where ``hqr_constants`` is not importable.  This module
provides the same ``get_hqr_home()`` and ``display_hqr_home()``
contracts as ``hqr_constants`` without requiring it on ``sys.path``.

When ``hqr_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``hqr_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``HQR_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from hqr_constants import display_hqr_home as display_hqr_home
    from hqr_constants import get_hqr_home as get_hqr_home
except (ModuleNotFoundError, ImportError):

    def get_hqr_home() -> Path:
        """Return the HQ Runtime home directory (default: ~/.hqr).

        Mirrors ``hqr_constants.get_hqr_home()``."""
        val = os.environ.get("HQR_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hqr"

    def display_hqr_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``hqr_constants.display_hqr_home()``."""
        home = get_hqr_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
