"""Filesystem locations that work both from source and from the frozen (MSI-installed) build."""

import os
import sys
from pathlib import Path

from . import APP_NAME


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def static_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent / "static"
    return Path(__file__).resolve().parent / "static"


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d
