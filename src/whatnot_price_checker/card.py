from __future__ import annotations

import cv2
import numpy as np


def _order_quad(pts: np.ndarray) -> np.ndarray:
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1)
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(diff)]
    bl = pts[np.argmax(diff)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def find_card_warp(
    frame_bgr: np.ndarray,
    *,
    min_area_ratio: float = 0.02,
    max_area_ratio: float = 0.95,
    out_width: int = 350,
) -> np.ndarray | None:
    """
    Find the largest quadrilateral contour and return a perspective-warped card.
    Pokemon cards are ~63:88 width:height; output height is derived from out_width.
    """
    h, w = frame_bgr.shape[:2]
    area_img = float(h * w)
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best_area = 0.0
    best_quad: np.ndarray | None = None

    for c in contours:
        peri = cv2.arcLength(c, True)
        if peri < 100:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) != 4:
            continue
        area = float(cv2.contourArea(approx))
        ratio = area / area_img
        if ratio < min_area_ratio or ratio > max_area_ratio:
            continue
        if area > best_area:
            best_area = area
            best_quad = approx.reshape(4, 2).astype(np.float32)

    if best_quad is None:
        return None

    ordered = _order_quad(best_quad)
    out_h = int(round(out_width * 88.0 / 63.0))
    dst = np.array(
        [[0, 0], [out_width - 1, 0], [out_width - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(ordered, dst)
    return cv2.warpPerspective(frame_bgr, m, (out_width, out_h))
