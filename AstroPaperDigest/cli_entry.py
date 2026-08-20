#!/usr/bin/env python3
"""Bundled CLI entry point, embedded inside the PyInstaller .app.

The desktop GUI (when frozen) launches this executable instead of
'python main.py' so the pipeline can run without requiring a system
Python install.  It also serves as the detached updater process
('--apply <marker.json>').

All user data resolves through src.paths (App Support in frozen mode).
"""

import os
import sys
from pathlib import Path

from src import paths

_DATA_DIR = paths.data_dir()
os.chdir(_DATA_DIR)
sys.path.insert(0, str(_DATA_DIR))


def _run_pipeline() -> None:
    import main  # bundled alongside this entry point

    main.main()


def _run_updater() -> None:
    from src import updater

    marker = Path(sys.argv[2])
    sys.exit(updater.main_apply(marker))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--apply":
        _run_updater()
    else:
        _run_pipeline()
