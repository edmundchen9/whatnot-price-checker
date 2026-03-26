"""
OCR for Pokémon cards on a live Whatnot stream.

Uses bounding box positions to identify text by card region:
  - Top ~22% of image  → card name (e.g. "Moltres ex")
  - Bottom ~18%         → collector number (e.g. "087/173")
  - Middle              → attacks/description (ignored for naming)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import numpy as np

_COLLECTOR_RE = re.compile(
    r"([A-Z]{0,4}\d{1,4}\s*/\s*[A-Z]{0,4}\d{1,4})"
    r"|"
    r"([A-Z]{2,5}\d{2,4})\b"
)
_JUNK_RE = re.compile(
    r"^\$?\d+(\.\d+)?\s*$"
    r"|^(bid|buy|now|viewer|sold|mint|nm|lp|hp|qty|shipping|taxes|winning|custom"
    r"|for\s+this|have|giveaway|auction|won|pre.?bid|near|singles"
    r"|renegade|card\s*shop|watchers?|bids?|comments?"
    r"|n.?foil|vidko|video|listing|foil|non.?foil"
    r"|zing|haue|hise|tHis|haVe)\b",
    re.I,
)
_HP_RE = re.compile(r"^\d+\s*HP$", re.I)
_STREAM_PHRASE_RE = re.compile(
    r"for\s+this\s+card|won\s+the\s+auction|pre.?bid|near\s+mint"
    r"|card\s+have|card\s+shop|giveaway|shipping\s+is",
    re.I,
)


@dataclass(frozen=True)
class OcrHit:
    text: str
    conf: float
    y_frac: float  # vertical center as fraction of image height (0=top, 1=bottom)


@dataclass(frozen=True)
class OcrResult:
    raw_lines: list[str]
    guessed_name: str
    collector_number: str
    script: str


_reader = None


def _get_reader():
    global _reader
    if _reader is not None:
        return _reader
    try:
        import easyocr
    except ImportError as e:
        raise RuntimeError(
            "OCR requires optional deps: pip install '.[ocr]' "
            "(installs EasyOCR and its dependencies)."
        ) from e
    _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


def _prepare(bgr: np.ndarray) -> np.ndarray:
    """Upscale + CLAHE for stream-quality frames."""
    h, w = bgr.shape[:2]
    target = 800
    if min(h, w) < target:
        scale = min(float(target) / float(min(h, w)), 3.0)
        bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    l = clahe.apply(l)
    bgr = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    return bgr


def _ocr_with_positions(bgr: np.ndarray) -> list[OcrHit]:
    """Single EasyOCR call; returns text with vertical position."""
    reader = _get_reader()
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    img_h = rgb.shape[0]
    try:
        raw = reader.readtext(
            rgb,
            detail=1,
            paragraph=False,
            text_threshold=0.25,
            low_text=0.20,
            link_threshold=0.30,
            mag_ratio=1.5,
            contrast_ths=0.05,
            adjust_contrast=0.7,
        )
    except TypeError:
        raw = reader.readtext(rgb, detail=1, paragraph=False)

    hits: list[OcrHit] = []
    for item in raw:
        if len(item) < 3:
            continue
        bbox, text, conf = item[0], str(item[1]).strip(), float(item[2])
        if not text or conf < 0.05:
            continue
        ys = [pt[1] for pt in bbox]
        y_center = (min(ys) + max(ys)) / 2.0
        y_frac = y_center / img_h if img_h > 0 else 0.5
        hits.append(OcrHit(text=text, conf=conf, y_frac=y_frac))
    return hits


def _is_junk(s: str) -> bool:
    s = s.strip()
    if len(s) < 2:
        return True
    if _JUNK_RE.search(s):
        return True
    if _HP_RE.match(s):
        return True
    if _STREAM_PHRASE_RE.search(s):
        return True
    if all(not ch.isalpha() for ch in s):
        return True
    letters = sum(ch.isalpha() for ch in s)
    if letters < 3:
        return True
    return False


NAME_ZONE_TOP = 0.22
COLLECTOR_ZONE_BOTTOM = 0.82


def _pick_name(hits: list[OcrHit]) -> str:
    """Pick the card name from text in the top region of the card."""
    top_hits = [h for h in hits if h.y_frac <= NAME_ZONE_TOP and not _is_junk(h.text)]

    if not top_hits:
        all_valid = [h for h in hits if not _is_junk(h.text)]
        if not all_valid:
            return ""
        top_hits = sorted(all_valid, key=lambda h: h.y_frac)[:3]

    def score(h: OcrHit) -> float:
        s = h.text
        sc = h.conf * 5.0
        sc -= h.y_frac * 10.0

        if re.match(r"^[A-Z][a-z]{2,15}$", s):
            sc += 12.0
        if re.search(r"\bex\b|\bEX\b|GX|VMAX|VSTAR|V$", s):
            sc += 6.0
        if re.search(r"BASIC|STAGE|POK[EÉ]MON|TRAINER|ENERGY", s, re.I):
            sc -= 3.0
        if re.search(r"(shop|watchers?|viewers?|renegade)", s, re.I):
            sc -= 15.0
        if _COLLECTOR_RE.search(s):
            sc -= 8.0
        if re.match(r"^\d", s):
            sc -= 5.0
        if len(s.split()) > 4:
            sc -= 5.0
        return sc

    best = max(top_hits, key=score)
    return best.text.strip()


def _pick_collector_number(hits: list[OcrHit]) -> str:
    """Extract collector number from text in the bottom region."""
    bottom_hits = [h for h in hits if h.y_frac >= COLLECTOR_ZONE_BOTTOM]
    for h in reversed(bottom_hits):
        m = _COLLECTOR_RE.search(h.text)
        if m:
            return (m.group(1) or m.group(2) or "").replace(" ", "")
    for h in reversed(hits):
        m = _COLLECTOR_RE.search(h.text)
        if m:
            return (m.group(1) or m.group(2) or "").replace(" ", "")
    return ""


def read_card(warped_bgr: np.ndarray) -> OcrResult:
    """
    Single-pass OCR using text position on the card.
    Top region → name, bottom region → collector number.
    """
    if warped_bgr.size == 0:
        return OcrResult(raw_lines=[], guessed_name="", collector_number="", script="unknown")

    prepped = _prepare(warped_bgr)
    hits = _ocr_with_positions(prepped)

    seen: set[str] = set()
    unique: list[OcrHit] = []
    for h in hits:
        k = h.text.casefold()
        if k not in seen:
            seen.add(k)
            unique.append(h)

    name = _pick_name(unique)
    collector = _pick_collector_number(unique)

    raw_lines = [f"{h.text} ({h.y_frac:.0%})" for h in unique[:10]]
    all_text = " ".join(h.text for h in unique)
    script = "ja" if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", all_text) else "en"

    return OcrResult(
        raw_lines=raw_lines,
        guessed_name=name,
        collector_number=collector,
        script=script,
    )
