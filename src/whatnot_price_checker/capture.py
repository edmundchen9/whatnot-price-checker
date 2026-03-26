from __future__ import annotations

from dataclasses import dataclass

import mss
import numpy as np

from whatnot_price_checker.win_window import WindowRect


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int


def grab_region(region: Region) -> np.ndarray:
    """Return BGR uint8 image (OpenCV native order)."""
    with mss.mss() as sct:
        box = {
            "left": region.left,
            "top": region.top,
            "width": region.width,
            "height": region.height,
        }
        shot = sct.grab(box)
        bgra = np.asarray(shot, dtype=np.uint8)
        return bgra[:, :, :3][:, :, ::-1].copy()


def region_from_window(rect: WindowRect) -> Region:
    return Region(
        left=rect.left,
        top=rect.top,
        width=max(1, rect.width),
        height=max(1, rect.height),
    )


def primary_monitor_region() -> Region:
    with mss.mss() as sct:
        m = sct.monitors[1]
        return Region(
            left=int(m["left"]),
            top=int(m["top"]),
            width=max(1, int(m["width"])),
            height=max(1, int(m["height"])),
        )
