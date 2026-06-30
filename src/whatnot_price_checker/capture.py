from __future__ import annotations

from dataclasses import dataclass

import mss
import numpy as np


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
