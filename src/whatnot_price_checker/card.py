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


def _crop_region(
    frame_bgr: np.ndarray,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
) -> np.ndarray:
    """Crop a fractional region from the frame."""
    h, w = frame_bgr.shape[:2]
    px0 = max(0, int(w * x0))
    px1 = min(w, int(w * x1))
    py0 = max(0, int(h * y0))
    py1 = min(h, int(h * y1))
    crop = frame_bgr[py0:py1, px0:px1]
    if crop.size == 0:
        return frame_bgr
    return crop


def _find_card_contour(
    frame_bgr: np.ndarray,
    *,
    min_area_ratio: float = 0.02,
    max_area_ratio: float = 0.95,
    out_width: int = 420,
) -> np.ndarray | None:
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


def _resize_to_card(frame_bgr: np.ndarray, out_width: int = 420) -> np.ndarray:
    """Resize any region to standard card dimensions for OCR."""
    out_h = int(round(out_width * 88.0 / 63.0))
    h, w = frame_bgr.shape[:2]
    interp = cv2.INTER_CUBIC if (h < out_h or w < out_width) else cv2.INTER_AREA
    return cv2.resize(frame_bgr, (out_width, out_h), interpolation=interp)


def warp_for_ocr(
    frame_bgr: np.ndarray,
    *,
    min_area_ratio: float = 0.02,
    max_area_ratio: float = 0.95,
    out_width: int = 420,
) -> tuple[np.ndarray | None, str]:
    """
    Try contour-based card detection on the frame.
    If that fails, resize the whole frame to card dimensions.
    Returns (image, source) with source 'outline', 'frame', or 'none'.
    """
    if frame_bgr.size == 0 or frame_bgr.shape[0] < 64 or frame_bgr.shape[1] < 64:
        return None, "none"

    outlined = _find_card_contour(
        frame_bgr,
        min_area_ratio=min_area_ratio,
        max_area_ratio=max_area_ratio,
        out_width=out_width,
    )
    if outlined is not None:
        return outlined, "outline"

    return _resize_to_card(frame_bgr, out_width), "frame"
