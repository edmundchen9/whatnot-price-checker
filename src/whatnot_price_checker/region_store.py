"""Persist the last-picked scan region so the app can skip the picker on launch.

Useful when the card area always shows up in the same place on screen (e.g. a
browser window that's reliably positioned), so you don't have to redraw the
region every time you start the app.
"""
from __future__ import annotations

import json
from pathlib import Path

from whatnot_price_checker.capture import Region

_REGION_FILE = Path.home() / ".whatnot_price_checker" / "region.json"


def save_region(region: Region) -> None:
    """Persist the region so it's restored automatically next launch."""
    try:
        _REGION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _REGION_FILE.write_text(
            json.dumps(
                {
                    "left": region.left,
                    "top": region.top,
                    "width": region.width,
                    "height": region.height,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # best-effort; falling back to the picker is fine


def load_region() -> Region | None:
    """Return the last-saved region, or None if missing/invalid/off-screen."""
    try:
        if not _REGION_FILE.exists():
            return None
        data = json.loads(_REGION_FILE.read_text(encoding="utf-8"))
        region = Region(
            left=int(data["left"]),
            top=int(data["top"]),
            width=int(data["width"]),
            height=int(data["height"]),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if not _region_fits_current_screens(region):
        return None
    return region


def clear_region() -> None:
    """Forget the saved region (forces the picker on next launch)."""
    try:
        _REGION_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _region_fits_current_screens(region: Region) -> bool:
    """Guard against a saved region from a monitor layout that no longer exists."""
    if region.width <= 0 or region.height <= 0:
        return False
    try:
        import mss

        with mss.mss() as sct:
            mon = sct.monitors[0]
        v_left, v_top = mon["left"], mon["top"]
        v_w, v_h = mon["width"], mon["height"]
        if v_w <= 0 or v_h <= 0:
            return True  # degenerate monitor info (e.g. no display attached); don't force a re-pick
        v_right, v_bottom = v_left + v_w, v_top + v_h
    except Exception:
        return True  # can't verify right now; don't force a re-pick over it
    return (
        region.left >= v_left
        and region.top >= v_top
        and region.left + region.width <= v_right
        and region.top + region.height <= v_bottom
    )
