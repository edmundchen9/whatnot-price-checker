"""
Google Cloud Vision Web Detection client.

Sends a card screenshot to the Vision API and returns:
  - best_label: the API's best guess at what the image shows
                (e.g. "charizard ex obsidian flames 125/197")
  - tcgplayer_url: first TCGPlayer product page found in matching pages, or ""
  - entities: top web-entity descriptions (e.g. ["Charizard ex", "Obsidian Flames"])

Requires GOOGLE_VISION_API_KEY in the environment / .env file.
First 1,000 requests/month are free; $3.50 per 1,000 beyond that.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any

import cv2
import httpx
import numpy as np

_VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
_TCGPLAYER_RE = re.compile(r"https?://(?:www\.)?tcgplayer\.com/\S+", re.I)

# Fraction of each edge to crop away before sending to Vision.
# This removes phone bezels, stream chat, "won the auction!" overlays, etc.
# that cause Vision to label the image "smartphone" / "screenshot".
_CROP_MARGIN = 0.10  # trim 10 % from each side

# Target longest dimension sent to Vision — large enough for detail, small
# enough to keep payload small and latency low.
_VISION_MAX_DIM = 640


@dataclass(frozen=True)
class VisionResult:
    best_label: str
    tcgplayer_url: str
    entities: list[str] = field(default_factory=list)


def _prepare_frame(bgr: np.ndarray) -> np.ndarray:
    """Crop margins and downscale before sending to Vision.

    Cropping removes phone chrome / chat / stream UI at the edges so Vision
    focuses on the card face. Downscaling reduces payload size and latency.
    """
    h, w = bgr.shape[:2]
    dy = max(1, int(h * _CROP_MARGIN))
    dx = max(1, int(w * _CROP_MARGIN))
    cropped = bgr[dy: h - dy, dx: w - dx]

    ch, cw = cropped.shape[:2]
    scale = _VISION_MAX_DIM / max(ch, cw)
    if scale < 1.0:
        new_w = max(1, int(cw * scale))
        new_h = max(1, int(ch * scale))
        cropped = cv2.resize(cropped, (new_w, new_h), interpolation=cv2.INTER_AREA)

    return cropped


class VisionClient:
    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._http = httpx.Client(timeout=15.0)

    def close(self) -> None:
        self._http.close()

    def analyze(self, bgr_frame: np.ndarray) -> VisionResult:
        """Send bgr_frame to Cloud Vision Web Detection.

        The frame is cropped and downscaled before transmission to reduce
        edge noise (stream UI, phone bezels) and lower latency.
        Returns a VisionResult; on any error returns an empty result.
        """
        try:
            return self._call(bgr_frame)
        except Exception as exc:
            print(f"[VISION] Error: {exc}")
            return VisionResult(best_label="", tcgplayer_url="", entities=[])

    def _call(self, bgr_frame: np.ndarray) -> VisionResult:
        prepared = _prepare_frame(bgr_frame)
        ok, buf = cv2.imencode(".jpg", prepared, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return VisionResult(best_label="", tcgplayer_url="", entities=[])

        b64 = base64.b64encode(buf.tobytes()).decode("ascii")

        payload: dict[str, Any] = {
            "requests": [
                {
                    "image": {"content": b64},
                    "features": [
                        {"type": "WEB_DETECTION", "maxResults": 10},
                    ],
                    # Hint Vision toward product/object matching rather than
                    # scene classification by providing a language hint.
                    "imageContext": {"webDetectionParams": {"includeGeoResults": False}},
                }
            ]
        }

        r = self._http.post(_VISION_URL, params={"key": self._key}, json=payload)
        r.raise_for_status()
        data = r.json()

        detection: dict = (
            data.get("responses", [{}])[0].get("webDetection") or {}
        )

        # Best label
        best_labels = detection.get("bestGuessLabels") or []
        best_label = (best_labels[0].get("label") or "").strip() if best_labels else ""

        # Top entity descriptions — lower threshold to catch more card names
        raw_entities = detection.get("webEntities") or []
        entities = [
            str(e.get("description") or "").strip()
            for e in raw_entities
            if e.get("description") and float(e.get("score") or 0) >= 0.35
        ]

        # First TCGPlayer URL from matching pages
        tcgplayer_url = ""
        for page in detection.get("pagesWithMatchingImages") or []:
            url = page.get("url") or ""
            if _TCGPLAYER_RE.match(url):
                tcgplayer_url = url
                break

        ch, cw = prepared.shape[:2]
        print(
            f"[VISION] {cw}×{ch} | label={best_label!r}  "
            f"entities={entities[:4]}  "
            f"tcgplayer={'yes' if tcgplayer_url else 'no'}"
        )
        return VisionResult(
            best_label=best_label,
            tcgplayer_url=tcgplayer_url,
            entities=entities,
        )
