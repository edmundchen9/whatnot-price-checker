"""
Dual-region OCR for Pokémon cards on a live Whatnot stream.

Strategy:
  1. Crop the TOP 25% of the card image → upscale → OCR → pick name.
  2. Crop the BOTTOM 8% of the card image → aggressive upscale + sharpen
     + binarize → OCR → pick collector number.
  3. Fuzzy-match the name against known dictionaries (English + Japanese kana
     from pokemon_names_ja_map.json and trainer_names_ja_map.json → English for API).
  EasyOCR loads English + Japanese models for JP card text.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

import cv2
import numpy as np

_COLLECTOR_RE = re.compile(
    r"(\d{1,4})\s*[/\\|lInN]\s*(\d{1,4})"
)
_COLLECTOR_PROMO_RE = re.compile(
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
class OcrResult:
    raw_lines: list[str]
    guessed_name: str
    collector_number: str
    script: str


# ---------------------------------------------------------------------------
# Pokémon name dictionary (loaded once)
# ---------------------------------------------------------------------------

_pokemon_names: list[str] | None = None
_ja_to_en: dict[str, str] | None = None
_ja_keys_list: list[str] | None = None

# Hiragana/Katakana/CJK + halfwidth kana + fullwidth alnum (e.g. Ｎ on JP N card)
_JAPANESE_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]"
)


def _has_japanese(s: str) -> bool:
    return bool(_JAPANESE_RE.search(s))


def _load_pokemon_names() -> list[str]:
    global _pokemon_names
    if _pokemon_names is not None:
        return _pokemon_names
    p = Path(__file__).with_name("pokemon_names.json")
    if p.exists():
        _pokemon_names = json.loads(p.read_text(encoding="utf-8"))
    else:
        _pokemon_names = []
    return _pokemon_names


def _load_ja_map() -> tuple[dict[str, str], list[str]]:
    """Japanese (kana/kanji) → English: Pokémon (PokeAPI) + trainers/items (curated)."""
    global _ja_to_en, _ja_keys_list
    if _ja_to_en is not None and _ja_keys_list is not None:
        return _ja_to_en, _ja_keys_list
    merged: dict[str, str] = {}
    for fname in ("pokemon_names_ja_map.json", "trainer_names_ja_map.json"):
        p = Path(__file__).with_name(fname)
        if p.exists():
            merged.update(json.loads(p.read_text(encoding="utf-8")))
    _ja_to_en = merged
    _ja_keys_list = list(_ja_to_en.keys())
    return _ja_to_en, _ja_keys_list


def _normalize_tcg_suffix(group: str) -> str:
    g = group.strip()
    fw = {"Ｖ": "V", "ＶＭＡＸ": "VMAX", "ＶＳＴＡＲ": "VSTAR"}
    if g in fw:
        return fw[g]
    low = g.lower()
    if low == "vmax":
        return "VMAX"
    if low == "vstar":
        return "VSTAR"
    if low == "gx":
        return "GX"
    if low == "v":
        return "V"
    if low == "ex":
        return "ex"
    return g


_TCG_SUFFIX_RE = re.compile(
    r"\s+(ex|EX|GX|V|VMAX|VSTAR|Ｖ|ＶＭＡＸ|ＶＳＴＡＲ)\s*$",
    re.I,
)


def fuzzy_match_name(ocr_text: str) -> str:
    """Return the closest card name, preserving any TCG suffix (ex/GX/V/etc.).

    Japanese Pokémon names are resolved to English canonical names for API lookup.
    """
    names = _load_pokemon_names()
    if not names:
        return ocr_text

    text = ocr_text.strip()
    suffix = ""
    m = _TCG_SUFFIX_RE.search(text)
    if m:
        suffix = " " + _normalize_tcg_suffix(m.group(1))
        text = text[: m.start()].strip()

    ja_map, ja_keys = _load_ja_map()
    if _has_japanese(text) and ja_map:
        if text in ja_map:
            return ja_map[text] + suffix
        jm = get_close_matches(text, ja_keys, n=1, cutoff=0.72)
        if jm:
            return ja_map[jm[0]] + suffix

    lower_map = {n.lower(): n for n in names}
    if text.lower() in lower_map:
        return lower_map[text.lower()] + suffix

    matches = get_close_matches(text, names, n=1, cutoff=0.55)
    if matches:
        return matches[0] + suffix

    matches = get_close_matches(text.title(), names, n=1, cutoff=0.55)
    if matches:
        return matches[0] + suffix

    for variant in [text, text.replace("'", ""), text.replace("'", "'")]:
        m2 = get_close_matches(variant, names, n=1, cutoff=0.50)
        if m2:
            return m2[0] + suffix

    return ocr_text


# ---------------------------------------------------------------------------
# EasyOCR singleton
# ---------------------------------------------------------------------------

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
    _reader = easyocr.Reader(["en", "ja"], gpu=False, verbose=False)
    return _reader


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def _upscale_clahe(bgr: np.ndarray, target_w: int = 600) -> np.ndarray:
    """Upscale to target width + CLAHE for contrast."""
    h, w = bgr.shape[:2]
    if w < target_w:
        scale = target_w / w
        bgr = cv2.resize(bgr, (target_w, int(h * scale)), interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    l = clahe.apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


# ---------------------------------------------------------------------------
# OCR call
# ---------------------------------------------------------------------------

def _ocr_region(bgr: np.ndarray) -> list[tuple[str, float]]:
    """Run EasyOCR on a BGR image; returns list of (text, confidence)."""
    reader = _get_reader()
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    try:
        raw = reader.readtext(
            rgb,
            detail=1,
            paragraph=False,
            text_threshold=0.30,
            low_text=0.25,
            link_threshold=0.30,
            mag_ratio=1.0,
            contrast_ths=0.05,
            adjust_contrast=0.7,
        )
    except TypeError:
        raw = reader.readtext(rgb, detail=1, paragraph=False)

    results: list[tuple[str, float]] = []
    for item in raw:
        if len(item) < 3:
            continue
        text = str(item[1]).strip()
        conf = float(item[2])
        if text and conf >= 0.05:
            results.append((text, conf))
    return results


# ---------------------------------------------------------------------------
# Junk filtering and name scoring
# ---------------------------------------------------------------------------

def _is_junk(s: str) -> bool:
    s = s.strip()
    if len(s) < 2:
        names = _load_pokemon_names()
        if s and any(n.lower() == s.lower() for n in names):
            return False
        ja_map, ja_keys = _load_ja_map()
        if s and ja_map and (s in ja_map or get_close_matches(s, ja_keys, n=1, cutoff=0.9)):
            return False
        return True
    if _JUNK_RE.search(s):
        return True
    if _HP_RE.match(s):
        return True
    if _STREAM_PHRASE_RE.search(s):
        return True
    ja_map, ja_keys = _load_ja_map()
    if ja_map and (s in ja_map or get_close_matches(s, ja_keys, n=1, cutoff=0.88)):
        return False
    if all(not ch.isalpha() for ch in s):
        return True
    if not _has_japanese(s) and sum(ch.isalpha() for ch in s) < 3:
        return True
    return False


def _is_known_name(text: str) -> bool:
    """Check if text closely matches any entry in the card name dictionary."""
    names = _load_pokemon_names()
    if not names:
        return False
    lower_map = {n.lower() for n in names}
    if text.lower() in lower_map:
        return True
    if get_close_matches(text, names, n=1, cutoff=0.65):
        return True
    ja_map, ja_keys = _load_ja_map()
    if ja_map and text in ja_map:
        return True
    if ja_map and get_close_matches(text, ja_keys, n=1, cutoff=0.72):
        return True
    return False


def _score_name(text: str, conf: float) -> float:
    sc = conf * 5.0
    if _is_known_name(text):
        sc += 14.0
    elif re.match(r"^[A-Z][a-z]{2,15}$", text):
        sc += 8.0
    if re.search(r"\bex\b|\bEX\b|GX|VMAX|VSTAR|V$|[ＶＶ]|ＶＭＡＸ|ＶＳＴＡＲ", text):
        sc += 6.0
    if re.match(r"^(BASIC|STAGE\s*\d?|POK[EÉ]MON|TRAINER|SUPPORTER|ITEM|STADIUM|ENERGY)$", text, re.I):
        sc -= 15.0
    if re.search(r"(shop|watchers?|viewers?|renegade)", text, re.I):
        sc -= 15.0
    if _COLLECTOR_RE.search(text) or _COLLECTOR_PROMO_RE.search(text):
        sc -= 8.0
    if re.match(r"^\d", text):
        sc -= 5.0
    if len(text.split()) > 5:
        sc -= 5.0
    return sc


def _pick_best_name(ocr_results: list[tuple[str, float]]) -> str:
    candidates = [(t, c) for t, c in ocr_results if not _is_junk(t)]
    if not candidates:
        return ""
    best_text, _ = max(candidates, key=lambda tc: _score_name(tc[0], tc[1]))
    return best_text.strip()


def _pick_collector_number(ocr_results: list[tuple[str, float]]) -> str:
    for text, _ in ocr_results:
        cleaned = text.replace(" ", "")
        m = _COLLECTOR_RE.search(cleaned)
        if m:
            num, total = m.group(1), m.group(2)
            num = num.lstrip("0") or "0"
            return f"{num}/{total}"
    for text, _ in ocr_results:
        m = _COLLECTOR_PROMO_RE.search(text)
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

NAME_CROP_FRAC = 0.25
COLLECTOR_CROP_FRAC = 0.08

_ocr_pool = ThreadPoolExecutor(max_workers=2)


def _ocr_strip(bgr_strip: np.ndarray) -> list[tuple[str, float]]:
    """Prep + OCR a single image strip."""
    prepped = _upscale_clahe(bgr_strip)
    return _ocr_region(prepped)


def _ocr_collector_strip(bgr_strip: np.ndarray) -> list[tuple[str, float]]:
    """More aggressive prep for the tiny collector-number text."""
    prepped = _upscale_clahe(bgr_strip, target_w=900)
    gray = cv2.cvtColor(prepped, cv2.COLOR_BGR2GRAY)
    sharp = cv2.filter2D(gray, -1, np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]]))
    _, binarized = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bgr_out = cv2.cvtColor(binarized, cv2.COLOR_GRAY2BGR)
    return _ocr_region(bgr_out)


def read_card(warped_bgr: np.ndarray, *, name_only: bool = False) -> OcrResult:
    """
    Dual-region OCR (runs name and collector in parallel):
      - Top 25% → card name
      - Bottom 8% → collector number (skipped when name_only=True)
    Then fuzzy-match the name against the Pokémon dictionary.

    Use name_only=True for the first scan to get a fast name read;
    once the name stabilizes, call again with name_only=False to also
    read the collector number before the API call.
    """
    if warped_bgr.size == 0:
        return OcrResult(raw_lines=[], guessed_name="", collector_number="", script="en")

    h, w = warped_bgr.shape[:2]
    name_strip = warped_bgr[0 : int(h * NAME_CROP_FRAC), :]

    if name_only:
        name_hits = _ocr_strip(name_strip)
        raw_name = _pick_best_name(name_hits)
        name = fuzzy_match_name(raw_name) if raw_name else ""
        raw_lines = [f"[name] {t} ({c:.0%})" for t, c in name_hits[:5]]
        scr = "ja" if raw_name and _has_japanese(raw_name) else "en"
        return OcrResult(raw_lines=raw_lines, guessed_name=name, collector_number="", script=scr)

    collector_strip = warped_bgr[int(h * (1.0 - COLLECTOR_CROP_FRAC)) :, :]

    name_future = _ocr_pool.submit(_ocr_strip, name_strip)
    coll_future = _ocr_pool.submit(_ocr_collector_strip, collector_strip)

    name_hits = name_future.result()
    collector_hits = coll_future.result()

    raw_name = _pick_best_name(name_hits)
    collector = _pick_collector_number(collector_hits)
    name = fuzzy_match_name(raw_name) if raw_name else ""
    scr = "ja" if raw_name and _has_japanese(raw_name) else "en"

    raw_lines = (
        [f"[name] {t} ({c:.0%})" for t, c in name_hits[:5]]
        + [f"[coll] {t} ({c:.0%})" for t, c in collector_hits[:5]]
    )

    return OcrResult(
        raw_lines=raw_lines,
        guessed_name=name,
        collector_number=collector,
        script=scr,
    )
