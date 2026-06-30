from __future__ import annotations

import cv2
import numpy as np

CARD_WIDTH = 420
CARD_ASPECT = 88.0 / 63.0  # standard Pokémon card H/W ratio


def resize_to_card(frame_bgr: np.ndarray, out_width: int = CARD_WIDTH) -> np.ndarray:
    """Resize a captured frame to standard card dimensions for OCR."""
    out_h = int(round(out_width * CARD_ASPECT))
    h, w = frame_bgr.shape[:2]
    interp = cv2.INTER_CUBIC if (h < out_h or w < out_width) else cv2.INTER_AREA
    return cv2.resize(frame_bgr, (out_width, out_h), interpolation=interp)


def warp_for_ocr(
    frame_bgr: np.ndarray,
    *,
    out_width: int = CARD_WIDTH,
) -> tuple[np.ndarray | None, str]:
    """
    Resize the user-selected region to card dimensions.
    Returns (image, source) where source is 'frame' or 'none'.
    """
    if frame_bgr.size == 0 or frame_bgr.shape[0] < 64 or frame_bgr.shape[1] < 64:
        return None, "none"

    return resize_to_card(frame_bgr, out_width), "frame"
