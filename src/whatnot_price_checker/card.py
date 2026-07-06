from __future__ import annotations

import cv2
import numpy as np

CARD_WIDTH = 420
CARD_ASPECT = 88.0 / 63.0  # standard Pokémon card H/W ratio
_CARD_RATIO = CARD_ASPECT  # ~1.397

# How far a candidate quad's long/short side ratio may stray from the real
# card ratio before we reject it — held cards are photographed at an angle,
# which foreshortens the perspective ratio, so this is intentionally loose.
_RATIO_MIN = 1.05
_RATIO_MAX = 1.85


def resize_to_card(frame_bgr: np.ndarray, out_width: int = CARD_WIDTH) -> np.ndarray:
    """Resize a captured frame to standard card dimensions for OCR."""
    out_h = int(round(out_width * CARD_ASPECT))
    h, w = frame_bgr.shape[:2]
    interp = cv2.INTER_CUBIC if (h < out_h or w < out_width) else cv2.INTER_AREA
    return cv2.resize(frame_bgr, (out_width, out_h), interpolation=interp)


def _order_quad_points(pts: np.ndarray) -> np.ndarray:
    """Sort 4 arbitrary corner points into [top-left, top-right, bottom-right, bottom-left]."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    diff = pts[:, 0] - pts[:, 1]
    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(s)]
    ordered[2] = pts[np.argmax(s)]
    ordered[1] = pts[np.argmax(diff)]
    ordered[3] = pts[np.argmin(diff)]
    return ordered


def _quad_candidates(frame_bgr: np.ndarray) -> list[np.ndarray]:
    """Find rectangle-ish contours in the frame that could plausibly be a card."""
    h, w = frame_bgr.shape[:2]
    frame_area = float(h * w)
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    candidates: list[np.ndarray] = []
    # Two edge-detection passes catch different lighting conditions — plain
    # Canny works well on crisp/high-contrast edges, while an adaptive
    # threshold on saturation/lightness can pick up sleeved cards with glare
    # that breaks up Canny's edges.
    edge_maps = [
        cv2.dilate(cv2.Canny(blurred, 40, 120), np.ones((3, 3), np.uint8), iterations=2),
        cv2.dilate(
            cv2.adaptiveThreshold(
                blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 5
            ),
            np.ones((3, 3), np.uint8),
            iterations=1,
        ),
    ]

    for edges in edge_maps:
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            # A held-up card should be a meaningfully large, but not
            # frame-filling, fraction of the shot.
            if area < 0.035 * frame_area or area > 0.90 * frame_area:
                continue
            rect = cv2.minAreaRect(c)
            (rw, rh) = rect[1]
            if rw < 1 or rh < 1:
                continue
            ratio = max(rw, rh) / min(rw, rh)
            if not (_RATIO_MIN <= ratio <= _RATIO_MAX):
                continue
            # Reject anything that spans almost the entire frame in both
            # dimensions — that's the video's own border/letterboxing, not
            # a card sitting inside the shot, even if its *area* (rounded
            # corners etc.) sneaks under the area cutoff above.
            x, y, bw, bh = cv2.boundingRect(c)
            if bw > 0.93 * w and bh > 0.93 * h:
                continue
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            quad = approx.reshape(-1, 2) if len(approx) == 4 else cv2.boxPoints(rect)
            candidates.append((area, quad.astype(np.float32)))

    candidates.sort(key=lambda t: t[0], reverse=True)
    return [q for _, q in candidates]


def _warp_quad(frame_bgr: np.ndarray, quad: np.ndarray, out_width: int) -> np.ndarray:
    ordered = _order_quad_points(quad)
    tl, tr, br, bl = ordered
    src_w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    src_h = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    out_h = int(round(out_width * CARD_ASPECT))

    if src_w > src_h:
        # Detected quad is wider than tall — the card is rotated ~90°
        # relative to upright, so map onto a landscape canvas then rotate
        # it back to portrait instead of squashing the aspect ratio.
        dst = np.array(
            [[0, 0], [out_h - 1, 0], [out_h - 1, out_width - 1], [0, out_width - 1]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(ordered, dst)
        warped = cv2.warpPerspective(frame_bgr, matrix, (out_h, out_width))
        warped = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)
    else:
        dst = np.array(
            [[0, 0], [out_width - 1, 0], [out_width - 1, out_h - 1], [0, out_h - 1]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(ordered, dst)
        warped = cv2.warpPerspective(frame_bgr, matrix, (out_width, out_h))
    return warped


def warp_for_ocr(
    frame_bgr: np.ndarray,
    *,
    out_width: int = CARD_WIDTH,
) -> tuple[np.ndarray | None, str]:
    """
    Locate the held card within a full captured frame and perspective-warp
    it to standard card dimensions for OCR.

    Returns (image, source):
      - source == "card": a rectangular card-shaped region was detected and
        perspective-corrected — the common, accurate path for full-stream
        frames (e.g. from the browser extension's video capture).
      - source == "frame": no confident card-shaped contour was found, so
        the whole input was resized as-is. This is also the only path used
        for the legacy desktop app, where the input is already a
        user-cropped region tightly framing just the card.
      - source == "none": input too small/empty to process.
    """
    if frame_bgr.size == 0 or frame_bgr.shape[0] < 64 or frame_bgr.shape[1] < 64:
        return None, "none"

    for quad in _quad_candidates(frame_bgr):
        try:
            warped = _warp_quad(frame_bgr, quad, out_width)
        except cv2.error:
            continue
        if warped.size == 0:
            continue
        return warped, "card"

    return resize_to_card(frame_bgr, out_width), "frame"
