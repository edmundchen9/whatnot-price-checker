"""Client for the JustTCG pricing API (https://justtcg.com).

JustTCG is the only one of our price sources that exposes real per-condition
(Near Mint / Lightly Played / Moderately Played / Heavily Played / Damaged)
prices — `tcgapi_client.py` and `tcgplayer.py` only return one price per
*printing* (Normal/Foil). This client is used to fill in the NM/LP/MP/HP/DM
condition grid in the overlay.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://api.justtcg.com/v1"

MAX_RETRIES = 2
RETRY_BACKOFF = [1.0, 3.0]

# JustTCG condition names -> the short labels shown in the overlay grid.
_CONDITION_LABELS: dict[str, str] = {
    "near mint": "NM",
    "lightly played": "LP",
    "moderately played": "MP",
    "heavily played": "HP",
    "damaged": "DM",
}
CONDITION_ORDER: tuple[str, ...] = ("NM", "LP", "MP", "HP", "DM")


class NetworkError(Exception):
    """Transient network/HTTP failure after retries."""


class RateLimitError(Exception):
    """Daily/monthly JustTCG quota exhausted."""


@dataclass(frozen=True)
class JustTcgVariant:
    condition: str  # short label, e.g. "NM"
    printing: str  # e.g. "Normal", "Holofoil", "Foil"
    price: float | None
    price_change_24h: float | None


@dataclass(frozen=True)
class JustTcgCard:
    id: str
    name: str
    set_name: str
    rarity: str
    tcgplayer_id: str | None
    variants: list[JustTcgVariant]


class JustTcgClient:
    def __init__(self, api_key: str) -> None:
        self._http = httpx.Client(
            timeout=30.0,
            headers={"x-api-key": api_key},
        )

    def close(self) -> None:
        self._http.close()

    def search(
        self, query: str, *, game: str = "pokemon", limit: int = 5
    ) -> list[JustTcgCard]:
        q = query.strip()
        if len(q) < 2:
            return []
        params = {
            # JustTCG's GET /v1/cards takes "q", not "query" — "query" is
            # silently ignored, which made every search fall back to the
            # API's unfiltered default listing (consistently topped by
            # Charizard Base Set, regardless of what was actually searched).
            "q": q,
            "game": game,
            "condition": "NM,LP,MP,HP,DMG",
            "limit": str(min(max(limit, 1), 20)),
        }

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self._http.get(f"{BASE_URL}/cards", params=params)
                if r.status_code == 429:
                    raise RateLimitError(
                        "JustTCG quota reached. Condition prices unavailable until reset.",
                    )
                r.raise_for_status()
                payload = r.json()
                rows = list(payload.get("data") or [])
                return [_parse_card(row) for row in rows]
            except RateLimitError:
                raise
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF[attempt])
        raise NetworkError(f"JustTCG request failed after {MAX_RETRIES + 1} attempts: {last_exc}") from last_exc

    def best_match(
        self, cards: list[JustTcgCard], collector_number: str
    ) -> JustTcgCard | None:
        """Pick the card whose number matches the OCR'd collector number.

        Without a usable number (common on promos, which rarely print one),
        fall back to a heuristic instead of blindly trusting search-result
        order: JustTCG seems to rank by something like price/popularity, so
        a common name like "Scorbunny" tends to surface its priciest chase
        print (e.g. a Shiny Vault holo) ahead of the much-more-likely-to-be-
        on-stream common/promo print. ``_disambiguate`` biases toward
        promo/common prints over known chase rarities, then newer sets, then
        lower price, as a best-effort guess — it's not a substitute for a
        real collector number when one is available.
        """
        if not cards:
            return None
        wanted = _collector_prefix(collector_number)
        if wanted:
            for card in cards:
                if _collector_prefix(_number_from_id(card.id)) == wanted:
                    return card
        return _disambiguate(cards)

    @staticmethod
    def conditions_for_printing(
        card: JustTcgCard, printing: str
    ) -> dict[str, float | None]:
        """Return {"NM": price, "LP": price, ...} for the given printing.

        Falls back to whichever printing has the most condition coverage if the
        requested printing has no variants (e.g. card only exists as Holofoil).
        """
        out: dict[str, float | None] = dict.fromkeys(CONDITION_ORDER, None)
        target = printing.strip().lower()
        matches = [v for v in card.variants if v.printing.strip().lower() == target]
        if not matches:
            by_printing: dict[str, list[JustTcgVariant]] = {}
            for v in card.variants:
                by_printing.setdefault(v.printing, []).append(v)
            if by_printing:
                matches = max(by_printing.values(), key=len)
        for v in matches:
            if v.condition in out:
                out[v.condition] = v.price
        return out

    @staticmethod
    def best_printing(card: JustTcgCard, *, prefer_foil: bool) -> str:
        printings = {v.printing for v in card.variants}
        if not printings:
            return "Holofoil" if prefer_foil else "Normal"
        if prefer_foil:
            for candidate in ("Holofoil", "Foil", "Reverse Holofoil"):
                if candidate in printings:
                    return candidate
            foil_like = [p for p in printings if "foil" in p.lower()]
            if foil_like:
                return foil_like[0]
        if "Normal" in printings:
            return "Normal"
        return sorted(printings)[0]


def _parse_card(row: dict[str, Any]) -> JustTcgCard:
    variants_raw = row.get("variants") or []
    variants: list[JustTcgVariant] = []
    for v in variants_raw:
        label = _CONDITION_LABELS.get(str(v.get("condition") or "").strip().lower())
        if not label:
            continue
        variants.append(
            JustTcgVariant(
                condition=label,
                printing=str(v.get("printing") or "").strip() or "Normal",
                price=_to_float(v.get("price")),
                price_change_24h=_to_float(v.get("priceChange24hr")),
            )
        )
    tcgplayer_id = row.get("tcgplayerId")
    return JustTcgCard(
        id=str(row.get("id") or ""),
        name=str(row.get("name") or ""),
        set_name=str(row.get("set_name") or ""),
        rarity=str(row.get("rarity") or ""),
        tcgplayer_id=str(tcgplayer_id) if tcgplayer_id else None,
        variants=variants,
    )


def _to_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Rarity/set-name substrings that flag a known "chase" (high-value, niche
# insert) print — deprioritized when guessing without a collector number.
_CHASE_KEYWORDS: tuple[str, ...] = (
    "shiny vault", "secret", "rainbow", "hyper rare", "alt art",
    "alternate art", "special illustration", "illustration rare",
    "gold rare", "full art", "shiny holo rare", "ultra rare", "ace spec",
    "crown zenith", "trophy",
)

# Rough chronological rank by set-id era prefix (higher = more recent).
# Anything unrecognized (promo lines like "first-partner-pack" that don't
# carry an era prefix) gets a neutral middle rank rather than being punished.
_ERA_RANKS: tuple[tuple[str, int], ...] = (
    ("me", 6),     # Mega Evolution era (current as of 2026)
    ("sv", 5),     # Scarlet & Violet
    ("swsh", 4),   # Sword & Shield
    ("sm", 3),     # Sun & Moon
    ("xy", 2),     # XY
)
_DEFAULT_ERA_RANK = 4


def _era_rank(card_id: str) -> int:
    slug = card_id.removeprefix("pokemon-")
    for prefix, rank in _ERA_RANKS:
        if slug.startswith(prefix):
            return rank
    return _DEFAULT_ERA_RANK


def _disambiguate(cards: list[JustTcgCard]) -> JustTcgCard:
    """Best-effort pick among same-name candidates when there's no collector
    number to go on — see ``JustTcgClient.best_match`` for the rationale."""

    def score(card: JustTcgCard) -> tuple[int, int, int, float]:
        haystack = f"{card.rarity} {card.set_name}".lower()
        is_chase = any(kw in haystack for kw in _CHASE_KEYWORDS)
        is_promo = "promo" in card.rarity.lower()
        nm_price = next(
            (v.price for v in card.variants if v.condition == "NM" and v.price is not None),
            None,
        )
        return (
            1 if is_chase else 0,
            0 if is_promo else 1,
            -_era_rank(card.id),
            nm_price if nm_price is not None else 0.0,
        )

    return min(cards, key=score)


def _normalize_number(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _collector_prefix(value: str) -> str:
    prefix = value.split("/", 1)[0]
    normalized = _normalize_number(prefix).lstrip("0")
    return normalized or _normalize_number(prefix)


def _number_from_id(card_id: str) -> str:
    """JustTCG card ids are slugs like '...-22-charizard-stamped-promo'; best-effort number
    extraction is not reliable, so this currently only supports exact numeric segments."""
    for part in card_id.split("-"):
        if part.isdigit():
            return part
    return ""
