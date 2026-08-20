"""Single source of truth for where AstroPaperDigest stores user data.

Two modes:

- Frozen (PyInstaller .app shipped via dmg): the bundle itself is read-only
  once installed (e.g. /Applications), so all writable data lives in
  ~/Library/Application Support/AstroPaperDigest (the same directory the
  GUI already used for its single-instance lock / run-info).
- Source (dev runs, Install.command channel, tests): data stays in the
  repository root, exactly as it always did.

Every module should call data_dir() instead of deriving the project
directory from __file__.
"""

from __future__ import annotations

import sys
from pathlib import Path

_APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "AstroPaperDigest"


def data_dir() -> Path:
    """Return the writable data directory, creating it if needed."""
    if getattr(sys, "frozen", False):
        data = _APP_SUPPORT_DIR
    else:
        data = Path(__file__).resolve().parent.parent
    data.mkdir(parents=True, exist_ok=True)
    return data
