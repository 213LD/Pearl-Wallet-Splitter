"""Path resolution helpers compatible with both source runs and PyInstaller builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_bundle_dir() -> Path:
    """Return directory containing bundled read-only resources."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def get_web_dir() -> Path:
    """Return path to the static web UI assets."""
    bundle = get_bundle_dir()
    candidate = bundle / "pearl_split" / "web"
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parent / "web"


def get_asset_path(filename: str) -> Path:
    """Return path to a bundled asset file."""
    bundle = get_bundle_dir()
    candidate = bundle / "assets" / filename
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parent.parent / "assets" / filename


def get_data_dir() -> Path:
    """Return writable directory for persistent SQLite data and exports.

    Prefers a portable ``data`` folder beside the executable (or repo root)
    when writable, falling back to ``%APPDATA%\\PearlMinerSplit``.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent

    portable_dir = base / "data"
    try:
        portable_dir.mkdir(parents=True, exist_ok=True)
        probe = portable_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return portable_dir
    except OSError:
        appdata = Path(os.environ.get("APPDATA", str(Path.home())))
        fallback = appdata / "PearlMinerSplit"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def get_db_path() -> Path:
    """Return SQLite database file path."""
    return get_data_dir() / "pearl_split.db"


def get_export_dir() -> Path:
    """Return default directory for CSV/JSON exports."""
    export_dir = get_data_dir() / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir
