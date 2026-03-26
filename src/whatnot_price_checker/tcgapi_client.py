from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import httpx


BASE_URL = "https://api.tcgapi.dev/v1"


@dataclass(frozen=True)
class TcgApiQuote:
    name: str
    set_name: str
    printing: str
    rarity: str
    number: str
    product_type: str
    foil_only: int
    market_price: float | None
    low_price: float | None
    median_price: float | None
    total_listings: int | None
    product_id: int | None
    rate_limit_remaining: int | None
    rate_limit_reset: str | None
    matched_preferred_printing: bool


class TcgApiClient:
    def __init__(self, api_key: str) -> None:
        self._http = httpx.Client(
            timeout=30.0,
            headers={"X-API-Key": api_key},
        )

    def close(self) -> None:
        self._http.close()

    def search_pokemon_cards(self, query: str, *, per_page: int = 25) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        q = query.strip()
        if len(q) < 2:
            return [], {}
        params = {
            "q": q,
            "game": "pokemon",
            "type": "Cards",
            "per_page": str(min(max(per_page, 1), 100)),
        }
        r = self._http.get(f"{BASE_URL}/search", params=params)
        r.raise_for_status()
        payload = r.json()
        rows = list(payload.get("data") or [])
        meta = payload.get("rate_limit") or {}
        return rows, meta

    def quote_from_search(
        self,
        query: str,
        *,
        prefer_foil: bool,
        per_page: int = 25,
    ) -> tuple[TcgApiQuote | None, dict[str, Any]]:
        rows, rl_meta = self.search_pokemon_cards(query, per_page=per_page)
        if not rows:
            return None, rl_meta

        pref = "Foil" if prefer_foil else "Normal"
        preferred = []
        for r in rows:
            printing = str(r.get("printing") or "").strip().lower()
            foil_only = _safe_int(r.get("foil_only")) == 1
            if printing == pref.lower() or (prefer_foil and foil_only):
                preferred.append(r)
        pool = preferred if preferred else rows
        row = pool[0]
        matched = bool(preferred)

        quote = TcgApiQuote(
            name=str(row.get("name") or ""),
            set_name=str(row.get("set_name") or ""),
            printing=str(row.get("printing") or pref),
            rarity=str(row.get("rarity") or ""),
            number=str(row.get("number") or ""),
            product_type=str(row.get("product_type") or ""),
            foil_only=_safe_int(row.get("foil_only")),
            market_price=_to_float(row.get("market_price")),
            low_price=_to_float(row.get("low_price")),
            median_price=_to_float(row.get("median_price")),
            total_listings=_to_int(row.get("total_listings")),
            product_id=_to_int(row.get("id")),
            rate_limit_remaining=_to_int(rl_meta.get("daily_remaining")),
            rate_limit_reset=str(rl_meta.get("daily_reset") or "") or None,
            matched_preferred_printing=matched,
        )
        return quote, rl_meta


def _to_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def tcgplayer_deep_link(name: str) -> str:
    """Manual price check when no API key (opens TCGPlayer search)."""
    return f"https://www.tcgplayer.com/search/pokemon/product?q={quote_plus(name)}&view=grid"
