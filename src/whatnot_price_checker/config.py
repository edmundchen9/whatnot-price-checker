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

    # substring match against window title (Windows)
    window_title_contains: str = "Whatnot"
    capture_fps: float = 3.0
    min_card_area_ratio: float = 0.02
    max_card_area_ratio: float = 0.95
    # tcgapi.dev (preferred when set)
    tcgapi_key: str | None = None
    tcgapi_per_page: int = 25
    price_cache_ttl_sec: float = 86400.0
    foil_ratio_threshold: float = 1.65
    # Video region crop — fraction of the captured browser window.
    # Whatnot puts the video player on the right; chat/sidebar is on the left.
    # Tweak with WPC_CROP_X0 / X1 / Y0 / Y1 if your layout differs.
    crop_x0: float = 0.38
    crop_x1: float = 1.0
    crop_y0: float = 0.05
    crop_y1: float = 0.92
    # overlay text refresh for noisy OCR (ms); prices/catalog still jump immediately when they change
    ui_refresh_ms: int = 450
    # TCGPlayer official API (optional fallback)
    tcgplayer_public_key: str | None = None
    tcgplayer_private_key: str | None = None
    tcgplayer_access_token: str | None = None
    tcg_api_version: str = "1.39.0"

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv_files()
        return cls(
            window_title_contains=os.environ.get(
                "WPC_WINDOW_TITLE", cls.window_title_contains
            ),
            capture_fps=float(os.environ.get("WPC_FPS", cls.capture_fps)),
            tcgapi_key=os.environ.get("TCGAPI_KEY") or os.environ.get("WPC_TCGAPI_KEY"),
            tcgapi_per_page=int(os.environ.get("WPC_TCGAPI_PER_PAGE", "25")),
            price_cache_ttl_sec=float(
                os.environ.get("WPC_PRICE_CACHE_TTL_SEC", cls.price_cache_ttl_sec)
            ),
            foil_ratio_threshold=float(
                os.environ.get("WPC_FOIL_RATIO_THRESHOLD", cls.foil_ratio_threshold)
            ),
            crop_x0=float(os.environ.get("WPC_CROP_X0", cls.crop_x0)),
            crop_x1=float(os.environ.get("WPC_CROP_X1", cls.crop_x1)),
            crop_y0=float(os.environ.get("WPC_CROP_Y0", cls.crop_y0)),
            crop_y1=float(os.environ.get("WPC_CROP_Y1", cls.crop_y1)),
            ui_refresh_ms=max(100, int(os.environ.get("WPC_UI_REFRESH_MS", cls.ui_refresh_ms))),
            tcgplayer_public_key=os.environ.get("TCGPLAYER_PUBLIC_KEY"),
            tcgplayer_private_key=os.environ.get("TCGPLAYER_PRIVATE_KEY"),
            tcgplayer_access_token=os.environ.get("TCGPLAYER_ACCESS_TOKEN"),
            tcg_api_version=os.environ.get("WPC_TCG_API_VERSION", cls.tcg_api_version),
        )
