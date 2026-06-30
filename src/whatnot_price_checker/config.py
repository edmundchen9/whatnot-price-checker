from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv_files() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")
    load_dotenv()


def normalize_price_lookup_key(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


@dataclass
class Settings:
    """Runtime settings; override with env vars where noted."""

    capture_fps: float = 8.0

    # tcgapi.dev (preferred when set)
    tcgapi_key: str | None = None
    tcgapi_per_page: int = 25
    price_cache_ttl_sec: float = 86400.0

    # overlay text refresh (ms)
    ui_refresh_ms: int = 450

    # Consecutive frames with the same OCR name before price lookup when Vision is absent.
    # With Vision enabled this is bypassed — Vision fires on frame 1 directly.
    stable_ticks_required: int = 1

    # How many consecutive empty/no-name frames must occur before the overlay is cleared.
    # Prevents a single blurry frame from wiping a good result off the screen.
    empty_ticks_before_reset: int = 4

    # Google Cloud Vision Web Detection (optional — for image-based card ID)
    google_vision_key: str | None = None

    # If True, tcgapi search uses Vision bestGuess when available (waits for Vision before search).
    # If False, Vision runs in parallel with tcgapi (OCR name); Vision is overlay-only for speed.
    vision_primary_for_search: bool = False

    # TCGPlayer official API (optional fallback)
    tcgplayer_public_key: str | None = None
    tcgplayer_private_key: str | None = None
    tcgplayer_access_token: str | None = None
    tcg_api_version: str = "1.39.0"

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv_files()
        return cls(
            capture_fps=float(os.environ.get("WPC_FPS", cls.capture_fps)),
            tcgapi_key=os.environ.get("TCGAPI_KEY") or os.environ.get("WPC_TCGAPI_KEY"),
            tcgapi_per_page=int(os.environ.get("WPC_TCGAPI_PER_PAGE", "25")),
            price_cache_ttl_sec=float(
                os.environ.get("WPC_PRICE_CACHE_TTL_SEC", cls.price_cache_ttl_sec)
            ),
            ui_refresh_ms=max(100, int(os.environ.get("WPC_UI_REFRESH_MS", cls.ui_refresh_ms))),
            stable_ticks_required=max(1, int(os.environ.get("WPC_STABLE_TICKS", cls.stable_ticks_required))),
            empty_ticks_before_reset=max(1, int(os.environ.get("WPC_EMPTY_TICKS_RESET", cls.empty_ticks_before_reset))),
            google_vision_key=os.environ.get("GOOGLE_VISION_API_KEY"),
            vision_primary_for_search=os.environ.get("WPC_VISION_PRIMARY_SEARCH", "").lower()
            in ("1", "true", "yes"),
            tcgplayer_public_key=os.environ.get("TCGPLAYER_PUBLIC_KEY"),
            tcgplayer_private_key=os.environ.get("TCGPLAYER_PRIVATE_KEY"),
            tcgplayer_access_token=os.environ.get("TCGPLAYER_ACCESS_TOKEN"),
            tcg_api_version=os.environ.get("WPC_TCG_API_VERSION", cls.tcg_api_version),
        )
