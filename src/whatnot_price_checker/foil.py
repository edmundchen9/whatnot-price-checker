from __future__ import annotations

import cv2
import numpy as np


def foil_art_texture_ratio(warped_bgr: np.ndarray) -> float:
    """
    Heuristic: holo / foil art tends to have higher Laplacian energy than the
    mostly-flat text bar. Returns center_lap / max(1, bottom_lap).
    """
    h, w = warped_bgr.shape[:2]
    if h < 40 or w < 40:
        return 1.0

    gray = cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2GRAY)
    lap = cv2.Laplacian(gray, cv2.CV_64F)

    c0, c1 = int(h * 0.22), int(h * 0.62)
    b0, b1 = int(h * 0.72), h
    x0, x1 = int(w * 0.12), int(w * 0.88)

    center = lap[c0:c1, x0:x1]
    bottom = lap[b0:b1, x0:x1]
    if center.size < 50 or bottom.size < 20:
        return 1.0

    c_var = float(center.var())
    b_var = float(bottom.var())
    return c_var / max(1.0, b_var)


def foil_likely(warped_bgr: np.ndarray, *, ratio_threshold: float = 1.65) -> bool:
    return foil_art_texture_ratio(warped_bgr) >= ratio_threshold
