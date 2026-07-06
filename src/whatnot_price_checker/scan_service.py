"""Headless, Qt-free OCR + price-lookup pipeline.

Extracted from ``app.py``'s ``ScanWorker`` so the exact same identification
and pricing logic (collector-number matching, JustTCG disambiguation, etc.)
can be reused by both the legacy desktop overlay and ``server.py`` — the
local API the browser extension talks to.

Deliberately leaves out the legacy tcgapi.dev / Google Vision paths and the
desktop app's price cache; the extension does its own request, and Vision
isn't configured for the primary use case this serves today.
"""
from __future__ import annotations

import base64
import time

import cv2
import numpy as np

from whatnot_price_checker.card import warp_for_ocr
from whatnot_price_checker.config import Settings
from whatnot_price_checker.justtcg_client import JustTcgClient
from whatnot_price_checker.ocr_reader import read_card
from whatnot_price_checker.tcgplayer import TcgClient

# Thumbnail max dimensions sent back to the extension/overlay for display —
# downscaled from the full warped crop since this is purely cosmetic.
_THUMB_MAX_W = 200
_THUMB_MAX_H = 280


def _thumb_data_url(bgr: np.ndarray) -> str:
    h, w = bgr.shape[:2]
    scale = min(_THUMB_MAX_W / w, _THUMB_MAX_H / h, 1.0)
    if scale < 1.0:
        bgr = cv2.resize(bgr, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def _blank_payload(warp_source: str) -> dict:
    return {
        "status": "card",
        "warp_source": warp_source,
        "warp_thumb": "",
        "ocr_name": "",
        "collector_number": "",
        "script": "en",
        "ocr_lines": "",
        "foil_guess": "Normal",
        "tcg_name": "",
        "set_name": "",
        "printing": "",
        "rarity": "",
        "card_number": "",
        "market": "",
        "low": "",
        "median": "",
        "detail": "",
        "lookup_stage": "",
        "condition_prices": {},
        "price_source": "",
        "lookup_ms": 0,
        "tcgplayer_id": "",
        "price_change_24h": None,
    }


def run_scan(
    frame_bgr: np.ndarray,
    *,
    settings: Settings,
    prefer_foil: bool = False,
    tcgplayer: TcgClient | None = None,
    justtcg: JustTcgClient | None = None,
) -> dict:
    """Identify the card in ``frame_bgr`` and look up its price.

    Returns a JSON-serializable dict shaped exactly like the overlay's
    snapshot payload (``tcg_name``, ``condition_prices``, etc.) so the same
    rendering logic works whether it came from the Qt overlay or the
    extension's content script.
    """
    warped, warp_source = warp_for_ocr(frame_bgr)
    if warped is None:
        base = _blank_payload("none")
        base["status"] = "idle"
        base["detail"] = "No card-shaped region found in the captured frame. Try again."
        return base

    ocr = read_card(warped, name_only=False)
    name = ocr.guessed_name.strip()
    collector = ocr.collector_number.strip()

    base = _blank_payload(warp_source)
    base["warp_thumb"] = _thumb_data_url(warped)
    base["ocr_name"] = name
    base["collector_number"] = collector
    base["script"] = ocr.script
    base["ocr_lines"] = " | ".join(ocr.raw_lines[:6])
    base["foil_guess"] = "Foil" if prefer_foil else "Normal"

    if not name:
        base["lookup_stage"] = "No card name detected in screenshot."
        return base

    if tcgplayer is None and justtcg is None:
        base["detail"] = (
            "Set TCGPLAYER_ACCESS_TOKEN/TCGPLAYER_PUBLIC_KEY+PRIVATE_KEY or "
            "JUSTTCG_API_KEY for prices."
        )
        base["lookup_stage"] = "No price API key configured."
        return base

    bundle: dict = {}
    if tcgplayer is not None:
        try:
            quote = tcgplayer.quote_best_match(
                name, collector_number=collector, prefer_foil=prefer_foil
            )
        except Exception as e:  # noqa: BLE001 - surfaced to the user as `detail`
            base["detail"] = str(e)
            quote = None
        if quote is not None:
            bundle = {
                "tcg_name": quote.name,
                "set_name": quote.set_name,
                "printing": quote.printing or ("Holofoil" if prefer_foil else "Normal"),
                "rarity": quote.rarity,
                "card_number": quote.number,
                "market": f"{quote.market_price:.2f}" if quote.market_price is not None else "—",
                "low": f"{quote.low_price:.2f}" if quote.low_price is not None else "—",
                "median": f"{quote.mid_price:.2f}" if quote.mid_price is not None else "—",
                "detail": "",
            }

    if justtcg is not None:
        _attach_justtcg(bundle, justtcg, search_name=name, collector=collector, prefer_foil=prefer_foil)

    if not bundle:
        base["detail"] = "No catalog match from any price source."
        base["lookup_stage"] = "No catalog match."
        return base

    base.update(bundle)
    base["lookup_stage"] = "Last lookup succeeded."
    return base


def _attach_justtcg(
    bundle: dict,
    justtcg: JustTcgClient,
    *,
    search_name: str,
    collector: str,
    prefer_foil: bool,
) -> None:
    """Fill the NM/LP/MP/HP/DM grid from JustTCG; backfill identity fields
    into ``bundle`` if TCGPlayer didn't already supply them."""
    start = time.perf_counter()
    try:
        cards = justtcg.search(search_name)
        card = justtcg.best_match(cards, collector)
    except Exception as e:  # noqa: BLE001
        bundle.setdefault("detail", f"JustTCG lookup failed: {e}")
        return
    if card is None:
        return

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    printing = JustTcgClient.best_printing(card, prefer_foil=prefer_foil)
    conditions = JustTcgClient.conditions_for_printing(card, printing)
    nm_variant = next(
        (v for v in card.variants if v.condition == "NM" and v.printing == printing),
        None,
    )

    bundle["condition_prices"] = conditions
    bundle["price_source"] = "JustTCG"
    bundle["lookup_ms"] = elapsed_ms
    if card.tcgplayer_id:
        bundle["tcgplayer_id"] = card.tcgplayer_id
    if nm_variant is not None:
        bundle["price_change_24h"] = nm_variant.price_change_24h

    if not bundle.get("tcg_name") and card.name:
        bundle["tcg_name"] = card.name
    if not bundle.get("set_name") and card.set_name:
        bundle["set_name"] = card.set_name
    if not bundle.get("rarity") and card.rarity:
        bundle["rarity"] = card.rarity
    if not bundle.get("printing"):
        bundle["printing"] = printing
    if not bundle.get("market") and conditions.get("NM") is not None:
        bundle["market"] = f"{conditions['NM']:.2f}"
