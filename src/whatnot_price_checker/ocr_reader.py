from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


@dataclass(frozen=True)
class OcrOutcome:
    raw_lines: list[str]
    guessed_name: str
    script: str  # "ja" | "en" | "unknown"


_reader = None


def _get_easyocr_reader():
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


def infer_script(text: str) -> str:
    if _CJK_RE.search(text):
        return "ja"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return "unknown"


def read_card_name(warped_bgr: np.ndarray) -> OcrOutcome:
    """OCR the upper portion of a warped card; pick a likely name line."""
    h, w = warped_bgr.shape[:2]
    crop = warped_bgr[0 : int(h * 0.35), :]
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)

    reader = _get_easyocr_reader()
    results = reader.readtext(rgb, detail=0, paragraph=False)
    lines = [str(t).strip() for t in results if str(t).strip()]

    if not lines:
        return OcrOutcome(raw_lines=[], guessed_name="", script="unknown")

    combined = "\n".join(lines)
    script = infer_script(combined)

    def score_line(s: str) -> tuple[int, float]:
        alnum = sum(ch.isalnum() for ch in s)
        noise = sum(ch in "|_[]" for ch in s)
        return (-noise, float(alnum))

    name = max(lines, key=score_line)
    return OcrOutcome(raw_lines=lines, guessed_name=name, script=script)
