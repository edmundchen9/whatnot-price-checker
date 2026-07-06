from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPoint, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from whatnot_price_checker import __version__
from whatnot_price_checker.capture import (
    Region,
    grab_region,
    has_screen_recording_access,
    request_screen_recording_access,
)
from whatnot_price_checker.card import warp_for_ocr
from whatnot_price_checker.config import Settings, normalize_price_lookup_key
from whatnot_price_checker.justtcg_client import CONDITION_ORDER, JustTcgClient
from whatnot_price_checker.justtcg_client import NetworkError as JustTcgNetworkError
from whatnot_price_checker.justtcg_client import RateLimitError as JustTcgRateLimitError
from whatnot_price_checker.ocr_reader import read_card
from whatnot_price_checker.region_store import load_region, save_region
from whatnot_price_checker.tcgapi_client import NetworkError, RateLimitError, TcgApiClient
from whatnot_price_checker.tcgplayer import TcgClient
from whatnot_price_checker.vision_client import VisionClient, VisionResult

# ---------------------------------------------------------------------------
# Vision label quality helpers
# ---------------------------------------------------------------------------

def _load_pokemon_name_set() -> frozenset[str]:
    """Lowercase set of all known Pokémon species names for entity matching."""
    p = Path(__file__).with_name("pokemon_names.json")
    try:
        names: list[str] = json.loads(p.read_text(encoding="utf-8"))
        return frozenset(n.strip().lower() for n in names if n.strip())
    except Exception:
        return frozenset()


_POKEMON_NAMES: frozenset[str] = _load_pokemon_name_set()

# Generic best-guess labels that describe the capture environment, not the card.
_VISION_LABEL_BLOCKLIST: frozenset[str] = frozenset({
    "screenshot", "screen shot", "smartphone", "smart phone",
    "mobile phone", "cellphone", "cell phone", "phone", "iphone",
    "android", "tablet", "tablet computer", "laptop", "computer",
    "poster", "flyer", "banner", "sign", "signage",
    "cartoon", "animation", "animated cartoon", "anime",
    "action figure", "figurine", "toy", "doll",
    "electronics", "electronic device", "consumer electronics",
    "technology", "gadget", "camera", "digital camera", "webcam",
    "darkness", "light", "font", "text", "paper", "rectangle",
    "product", "publication", "book", "display device", "monitor",
    "television", "multimedia", "material property", "graphic",
    "graphic design", "illustration", "art", "artwork",
})

# Noise suffixes appended by Web Detection that confuse tcgapi searches.
_LABEL_STRIP_SUFFIXES: tuple[str, ...] = (
    " pokemon tcg card", " pokemon tcg", " pokemon card",
    " trading card game", " trading card", " card game",
    " tcg card", " tcg", " pokemon", " ex card", " gx card",
)

# Words that mark a web entity as generic / hardware / non-card.
_ENTITY_BLOCKLIST_WORDS: frozenset[str] = frozenset({
    "smartphone", "mobile", "phone", "blackberry", "samsung",
    "apple", "iphone", "android", "nokia", "motorola",
    "electronics", "technology", "television", "computer",
    "camera", "screen", "display", "monitor", "tablet",
    "trading card", "card game", "pocket", "tcg",
    "figure", "figurine", "toy", "action",
})


def _pick_vision_search_name(vis_result: VisionResult, ocr_name: str) -> str:
    """Return the best tcgapi search term derived from Vision + OCR.

    Priority:
      1. Entity that is a known Pokémon name (highest confidence signal).
      2. Vision best-guess label after stripping noise suffixes, if not blocked.
      3. Any short web entity that doesn't look like generic hardware/tech.
      4. OCR-derived name as final fallback.
    """
    # --- Pass 1: look for a known Pokémon species in the entity list first.
    # Web entities are sorted by confidence by Google, so first match wins.
    for entity in vis_result.entities:
        e = entity.strip()
        if not e:
            continue
        # Single Pokémon name or "Name ex / Name V / Name GX" etc.
        first_word = e.split()[0].lower()
        if first_word in _POKEMON_NAMES:
            return e  # e.g. "Dustox" or "Charizard ex"

    # --- Pass 2: cleaned best-guess label.
    raw_label = (vis_result.best_label or "").strip().lower()
    cleaned = raw_label
    for suffix in _LABEL_STRIP_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break

    if cleaned and cleaned not in _VISION_LABEL_BLOCKLIST:
        # Extra check: if the first word of the cleaned label is a known Pokémon
        # name, use the cleaned label as-is (e.g. "dustox" after stripping " tcg").
        return cleaned

    # --- Pass 3: any short, non-generic entity.
    for entity in vis_result.entities:
        e = entity.strip()
        if not e:
            continue
        words = e.lower().split()
        if len(words) > 3:
            continue
        if any(w in _ENTITY_BLOCKLIST_WORDS for w in words):
            continue
        return e

    # --- Pass 4: OCR.
    return ocr_name


def _compact_tcgplayer_search_name(candidate: str, fallback: str) -> str:
    """Keep Vision searches compatible with TCGPlayer's product-name filter."""
    words = candidate.strip().split()
    if not words:
        return fallback
    first = words[0].strip(":-/#").lower()
    if first not in _POKEMON_NAMES:
        return fallback
    out = [words[0]]
    if len(words) > 1 and words[1].strip(":-/#").lower() in {"ex", "gx", "v", "vmax", "vstar"}:
        out.append(words[1])
    return " ".join(out)


# ---------------------------------------------------------------------------
# Overlay display helpers (confidence/price tags, thumbnail, condition matching)
# ---------------------------------------------------------------------------

def _normalize_collector(value: str) -> str:
    prefix = value.split("/", 1)[0]
    digits = "".join(ch for ch in prefix.lower() if ch.isalnum()).lstrip("0")
    return digits


def _collector_matches(a: str, b: str) -> bool:
    na, nb = _normalize_collector(a), _normalize_collector(b)
    return bool(na) and na == nb


def _confidence_label(data: dict) -> tuple[str, str]:
    """Return (label, color) describing how trustworthy the current match is."""
    if not (data.get("tcg_name") or "").strip():
        return "NO MATCH", "#8a8a98"
    collector = (data.get("collector_number") or "").strip()
    card_number = (data.get("card_number") or "").strip()
    detail = (data.get("detail") or "").strip()
    if collector and card_number and _collector_matches(collector, card_number) and not detail:
        return "HIGH CONFIDENCE", "#3ddc84"
    if collector and card_number and not _collector_matches(collector, card_number):
        return "LOW CONFIDENCE", "#e0744d"
    return "MEDIUM CONFIDENCE", "#e0c34d"


def _price_tier_label(market: str) -> str:
    try:
        val = float(market)
    except (TypeError, ValueError):
        return ""
    if val < 5:
        return "Bulk <$5"
    if val < 25:
        return "Mid $5–$25"
    return "Chase $25+"


def _bgr_to_pixmap(bgr: np.ndarray, width: int, height: int) -> QPixmap:
    """Convert a captured BGR frame into a scaled QPixmap thumbnail."""
    rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    h, w = rgb.shape[:2]
    qimg = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
    pix = QPixmap.fromImage(qimg)
    return pix.scaled(
        width,
        height,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _rounded_pixmap(pixmap: QPixmap, radius: int) -> QPixmap:
    """Clip a pixmap to rounded corners (for the card thumbnail)."""
    if pixmap.isNull():
        return pixmap
    size = pixmap.size()
    out = QPixmap(size)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size.width(), size.height(), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return out


class ScanWorker(QThread):
    snapshot = Signal(dict)

    def __init__(self, settings: Settings, scan_region: Region, *, prefer_foil: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._scan_region = scan_region
        self._prefer_foil = prefer_foil
        self._stop = False
        self._stable_name: str | None = None
        self._stable_ticks = 0
        self._empty_ticks = 0          # consecutive frames with no detected name
        self._cache_key: str | None = None
        self._cache_until = 0.0
        self._cache_bundle: dict = {}
        self._api_http_calls = 0

    def set_foil_preference(self, foil: bool) -> None:
        self._prefer_foil = foil

    def stop(self) -> None:
        self._stop = True
        self.requestInterruption()

    def run(self) -> None:
        tcgplayer: TcgClient | None = None
        has_token = bool(self._settings.tcgplayer_access_token)
        has_keys = bool(
            self._settings.tcgplayer_public_key and self._settings.tcgplayer_private_key
        )
        if has_token or has_keys:
            tcgplayer = TcgClient(self._settings)

        vision: VisionClient | None = None
        if self._settings.google_vision_key:
            vision = VisionClient(self._settings.google_vision_key)

        justtcg: JustTcgClient | None = None
        if self._settings.justtcg_api_key:
            justtcg = JustTcgClient(self._settings.justtcg_api_key)

        try:
            try:
                self._tick(None, tcgplayer, vision, justtcg)
            except Exception as e:
                self._emit_snapshot({"status": "error", "detail": str(e)})
        finally:
            if tcgplayer is not None:
                tcgplayer.close()
            if vision is not None:
                vision.close()
            if justtcg is not None:
                justtcg.close()

    def _sleep_cancellable(self, total_ms: int) -> None:
        remaining = total_ms
        while remaining > 0 and not self._stop and not self.isInterruptionRequested():
            chunk = min(50, remaining)
            self.msleep(chunk)
            remaining -= chunk

    def _tick(
        self,
        tcgapi: TcgApiClient | None,
        tcgplayer: TcgClient | None,
        vision: VisionClient | None = None,
        justtcg: JustTcgClient | None = None,
    ) -> None:
        if self._stop or self.isInterruptionRequested():
            return
        frame = grab_region(self._scan_region)
        warped, warp_source = warp_for_ocr(frame)
        if warped is None:
            self._reset_stable()
            self._emit_snapshot({"status": "idle", "detail": "No card-shaped region found in screenshot. Press W to try again."})
            return

        try:
            ocr = read_card(warped, name_only=False)
        except RuntimeError as e:
            self._emit_snapshot({"status": "ocr_missing", "detail": str(e)})
            return

        name = ocr.guessed_name.strip()
        collector = ocr.collector_number.strip()
        foil = self._prefer_foil

        lines_preview = " | ".join(ocr.raw_lines[:6])
        print(f"[OCR] warp={warp_source}  name={name!r}  collector={collector!r}  "
              f"foil={'Y' if foil else 'N'}  raw={lines_preview}")

        base: dict = {
            "status": "card",
            "warp_source": warp_source,
            "ocr_name": name,
            "collector_number": collector,
            "script": ocr.script,
            "ocr_lines": lines_preview,
            "foil_guess": "Foil" if foil else "Normal",
            "tcg_name": "",
            "set_name": "",
            "printing": "",
            "rarity": "",
            "card_number": "",
            "product_type": "",
            "catalog_foil_only": "",
            "listings": "",
            "market": "",
            "low": "",
            "median": "",
            "rate_remaining": "",
            "rate_reset": "",
            "detail": "",
            "lookup_stage": "",
            "vision_label": "",
            "vision_tcgplayer_url": "",
            "condition_prices": {},
            "price_source": "",
            "lookup_ms": 0,
            "tcgplayer_id": "",
            "price_change_24h": None,
            "warp_frame": warped,
        }

        if not name:
            base["lookup_stage"] = "No card name detected in screenshot."
            print("[SCAN] No name detected, skipping API lookup.")
            self._emit_snapshot(base)
            return

        self._empty_ticks = 0
        is_new_card = name != self._stable_name
        if is_new_card:
            self._stable_name = name
            self._stable_ticks = 1
        else:
            self._stable_ticks += 1

        if tcgplayer is None and justtcg is None:
            base["detail"] = (
                "Set TCGPLAYER_ACCESS_TOKEN/TCGPLAYER_PUBLIC_KEY+PRIVATE_KEY or "
                "JUSTTCG_API_KEY for prices."
            )
            base["lookup_stage"] = "No price API key configured."
            self._emit_snapshot(base)
            return

        search_name = name
        if vision is not None:
            vision_result = self._fetch_vision(vision, base, warped)
            search_name = _compact_tcgplayer_search_name(
                _pick_vision_search_name(vision_result, name),
                name,
            )
            if search_name != name:
                base["lookup_stage"] = f"Vision search: {search_name!r}"

        lk = f"{normalize_price_lookup_key(search_name)}|{collector}|{int(foil)}"
        now = time.time()
        if lk == self._cache_key and now < self._cache_until:
            base["lookup_stage"] = "Cached"
            self._emit_snapshot({**base, **self._cache_bundle})
            return

        self._fetch_tcgplayer(tcgplayer, base, lk, now, search_name=search_name, justtcg=justtcg)

    def _emit_snapshot(self, payload: dict) -> None:
        out = dict(payload)
        out["api_http_calls"] = self._api_http_calls
        out["has_tcgapi_key"] = bool(self._settings.tcgapi_key)
        out["has_tcgplayer_key"] = bool(
            self._settings.tcgplayer_access_token
            or (self._settings.tcgplayer_public_key and self._settings.tcgplayer_private_key)
        )
        self.snapshot.emit(out)

    def _fetch_vision(
        self, client: VisionClient, base: dict, frame
    ) -> VisionResult:
        """Call Vision API and write results into base in-place."""
        print("[VISION] Sending frame to Cloud Vision Web Detection…")
        result = client.analyze(frame)
        if result.best_label:
            base["vision_label"] = result.best_label
        if result.tcgplayer_url:
            base["vision_tcgplayer_url"] = result.tcgplayer_url
        return result

    def _parallel_vision_and_tcgapi(
        self,
        tcgapi: TcgApiClient,
        vision: VisionClient,
        base: dict,
        lk: str,
        now: float,
        warped,
    ) -> None:
        """Run Google Vision Web Detection and tcgapi.dev search concurrently (lower latency)."""
        base["lookup_stage"] = "Vision + tcgapi (parallel)…"
        self._emit_snapshot(dict(base))

        def vision_task():
            print("[VISION] parallel Web Detection…")
            return vision.analyze(warped)

        def tcgapi_task():
            ocr_name = base["ocr_name"]
            collector = base.get("collector_number", "")
            search_q = f"{ocr_name} {collector}".strip() if collector else ocr_name
            calls = 0
            try:
                calls += 1
                quote, meta = tcgapi.quote_from_search(
                    search_q,
                    prefer_foil=self._prefer_foil,
                    per_page=self._settings.tcgapi_per_page,
                )
                if quote is None and collector:
                    calls += 1
                    quote, meta = tcgapi.quote_from_search(
                        ocr_name,
                        prefer_foil=self._prefer_foil,
                        per_page=self._settings.tcgapi_per_page,
                    )
                return ("ok", quote, meta, calls, search_q, ocr_name, None)
            except RateLimitError as e:
                return ("rate", None, None, calls, search_q, ocr_name, e)
            except NetworkError as e:
                return ("net", None, None, calls, search_q, ocr_name, e)
            except Exception as e:
                return ("err", None, None, calls, search_q, ocr_name, e)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_v = pool.submit(vision_task)
            fut_t = pool.submit(tcgapi_task)
            vres = fut_v.result()
            tapi = fut_t.result()

        self._api_http_calls += tapi[3]

        if vres.best_label:
            base["vision_label"] = vres.best_label
        if vres.tcgplayer_url:
            base["vision_tcgplayer_url"] = vres.tcgplayer_url

        kind, quote, _meta, _calls, search_q, _ocr_name, err = tapi
        if kind == "rate":
            base["detail"] = str(err)
            base["lookup_stage"] = "Rate limited — using cached prices only."
            self._emit_snapshot(base)
            return
        if kind == "net":
            base["detail"] = f"Network error (retried): {err}"
            base["lookup_stage"] = "Network error — will retry next scan."
            self._emit_snapshot(base)
            return
        if kind == "err":
            base["detail"] = str(err)
            base["lookup_stage"] = "Search failed."
            self._emit_snapshot(base)
            return

        if quote is None:
            print(f"[API] No results for {search_q!r}")
            base["detail"] = "No results for this card name."
            base["lookup_stage"] = "No results."
            self._emit_snapshot(base)
            return

        print(f"[API] Found: {quote.name} | {quote.set_name} | "
              f"${quote.market_price} | {quote.printing}")
        foil_only_lbl = "yes (SKU is foil-only)" if quote.foil_only == 1 else "no"
        listings_lbl = (
            str(quote.total_listings) if quote.total_listings is not None else ""
        )
        bundle = {
            "tcg_name": quote.name,
            "set_name": quote.set_name,
            "printing": quote.printing,
            "rarity": quote.rarity,
            "card_number": quote.number,
            "product_type": quote.product_type,
            "catalog_foil_only": foil_only_lbl,
            "listings": listings_lbl,
            "market": f"{quote.market_price:.2f}" if quote.market_price is not None else "—",
            "low": f"{quote.low_price:.2f}" if quote.low_price is not None else "—",
            "median": f"{quote.median_price:.2f}" if quote.median_price is not None else "—",
            "rate_remaining": str(quote.rate_limit_remaining)
            if quote.rate_limit_remaining is not None
            else "",
            "rate_reset": quote.rate_limit_reset or "",
            "detail": "",
        }
        if not quote.matched_preferred_printing:
            want = "Foil" if self._prefer_foil else "Normal"
            bundle["detail"] = (
                f"No {want} row in top results; showing closest listing ({quote.printing})."
            )
        self._store_cache(lk, now, base, bundle)

    def _store_cache(self, key: str, now: float, base: dict, bundle: dict) -> None:
        self._cache_key = key
        self._cache_until = now + self._settings.price_cache_ttl_sec
        # Preserve Vision results in the cache bundle so they survive future
        # cache hits without re-calling the Vision API.
        for vk in ("vision_label", "vision_tcgplayer_url"):
            if base.get(vk) and vk not in bundle:
                bundle[vk] = base[vk]
        bundle = {**bundle, "lookup_stage": "Last lookup succeeded; prices cached for this card."}
        self._cache_bundle = bundle
        self._emit_snapshot({**base, **bundle})

    def _fetch_tcgapi(
        self,
        client: TcgApiClient,
        base: dict,
        lk: str,
        now: float,
        *,
        search_name: str | None = None,
    ) -> None:
        self._api_http_calls += 1
        name = (search_name or base["ocr_name"]).strip() or base["ocr_name"]
        collector = base.get("collector_number", "")

        search_q = f"{name} {collector}".strip() if collector else name
        print(f"[API] tcgapi.dev search: {search_q!r}  (call #{self._api_http_calls})")
        base["lookup_stage"] = f"Searching: {search_q!r} …"
        self._emit_snapshot(dict(base))

        try:
            quote, _meta = client.quote_from_search(
                search_q,
                prefer_foil=self._prefer_foil,
                per_page=self._settings.tcgapi_per_page,
            )
            if quote is None and collector:
                self._api_http_calls += 1
                print(f"[API] No results with collector#; retrying name-only: {name!r}")
                base["detail"] = "Collector# didn't narrow results — trying name only."
                self._emit_snapshot(dict(base))
                quote, _meta = client.quote_from_search(
                    name,
                    prefer_foil=self._prefer_foil,
                    per_page=self._settings.tcgapi_per_page,
                )
        except RateLimitError as e:
            print(f"[API] RATE LIMITED: {e}")
            base["detail"] = str(e)
            base["lookup_stage"] = "Rate limited — using cached prices only."
            self._emit_snapshot(base)
            return
        except NetworkError as e:
            print(f"[API] NETWORK ERROR: {e}")
            base["detail"] = f"Network error (retried): {e}"
            base["lookup_stage"] = "Network error — will retry next scan."
            self._emit_snapshot(base)
            return
        except Exception as e:
            print(f"[API] ERROR: {e}")
            base["detail"] = str(e)
            base["lookup_stage"] = "Search failed."
            self._emit_snapshot(base)
            return

        if quote is None:
            print(f"[API] No results for {search_q!r}")
            base["detail"] = "No results for this card name."
            base["lookup_stage"] = "No results."
            self._emit_snapshot(base)
            return

        print(f"[API] Found: {quote.name} | {quote.set_name} | "
              f"${quote.market_price} | {quote.printing}")
        foil_only_lbl = "yes (SKU is foil-only)" if quote.foil_only == 1 else "no"
        listings_lbl = (
            str(quote.total_listings) if quote.total_listings is not None else ""
        )
        bundle = {
            "tcg_name": quote.name,
            "set_name": quote.set_name,
            "printing": quote.printing,
            "rarity": quote.rarity,
            "card_number": quote.number,
            "product_type": quote.product_type,
            "catalog_foil_only": foil_only_lbl,
            "listings": listings_lbl,
            "market": f"{quote.market_price:.2f}" if quote.market_price is not None else "—",
            "low": f"{quote.low_price:.2f}" if quote.low_price is not None else "—",
            "median": f"{quote.median_price:.2f}" if quote.median_price is not None else "—",
            "rate_remaining": str(quote.rate_limit_remaining)
            if quote.rate_limit_remaining is not None
            else "",
            "rate_reset": quote.rate_limit_reset or "",
            "detail": "",
        }
        if not quote.matched_preferred_printing:
            want = "Foil" if self._prefer_foil else "Normal"
            bundle["detail"] = (
                f"No {want} row in top results; showing closest listing ({quote.printing})."
            )
        self._store_cache(lk, now, base, bundle)

    def _fetch_tcgplayer(
        self,
        client: TcgClient | None,
        base: dict,
        lk: str,
        now: float,
        *,
        search_name: str | None = None,
        justtcg: JustTcgClient | None = None,
    ) -> None:
        name = (search_name or base["ocr_name"]).strip() or base["ocr_name"]
        collector = (base.get("collector_number") or "").strip()
        bundle: dict = {}

        if client is not None:
            self._api_http_calls += 1
            base["lookup_stage"] = f"TCGPlayer search: {name!r} (shot #{self._api_http_calls})…"
            self._emit_snapshot(dict(base))
            try:
                quote = client.quote_best_match(
                    name,
                    collector_number=collector,
                    prefer_foil=self._prefer_foil,
                )
            except Exception as e:
                base["detail"] = str(e)
                base["lookup_stage"] = "TCGPlayer request failed."
                self._emit_snapshot(base)
                quote = None

            if quote is None and justtcg is None:
                base["detail"] = "No TCGPlayer catalog match."
                base["lookup_stage"] = "No catalog match (call still counted)."
                self._emit_snapshot(base)
                return

            if quote is not None:
                bundle = {
                    "tcg_name": quote.name,
                    "set_name": quote.set_name,
                    "printing": quote.printing or ("Holofoil" if self._prefer_foil else "Normal"),
                    "rarity": quote.rarity,
                    "card_number": quote.number,
                    "product_type": "",
                    "catalog_foil_only": "",
                    "listings": "",
                    "market": f"{quote.market_price:.2f}" if quote.market_price is not None else "—",
                    "low": f"{quote.low_price:.2f}" if quote.low_price is not None else "—",
                    "median": f"{quote.mid_price:.2f}" if quote.mid_price is not None else "—",
                    "rate_remaining": "",
                    "rate_reset": "",
                    "detail": "",
                }

        self._attach_justtcg(bundle, justtcg, search_name=name, collector=collector)

        if not bundle:
            base["detail"] = "No catalog match from any price source."
            base["lookup_stage"] = "No catalog match."
            self._emit_snapshot(base)
            return

        self._store_cache(lk, now, base, bundle)

    def _attach_justtcg(
        self,
        bundle: dict,
        justtcg: JustTcgClient | None,
        *,
        search_name: str,
        collector: str,
    ) -> None:
        """Fill the NM/LP/MP/HP/DM condition grid from JustTCG (the only source that has it).

        Also backfills name/set/rarity/market price into ``bundle`` when no other
        backend supplied them (e.g. TCGPlayer isn't configured).
        """
        if justtcg is None:
            return
        start = time.perf_counter()
        try:
            cards = justtcg.search(search_name)
            card = justtcg.best_match(cards, collector)
        except JustTcgRateLimitError as e:
            bundle.setdefault("detail", str(e))
            return
        except JustTcgNetworkError as e:
            bundle.setdefault("detail", f"JustTCG network error: {e}")
            return
        except Exception as e:
            bundle.setdefault("detail", f"JustTCG lookup failed: {e}")
            return

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if card is None:
            return

        printing = JustTcgClient.best_printing(card, prefer_foil=self._prefer_foil)
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
        nm_price = conditions.get("NM")
        if (not bundle.get("market") or bundle.get("market") == "—") and nm_price is not None:
            bundle["market"] = f"{nm_price:.2f}"

    def _vision_first_tick(
        self,
        tcgapi: TcgApiClient,
        vision: VisionClient,
        base: dict,
        lk: str,
        now: float,
        warped,
    ) -> None:
        """Run Google Vision Web Detection and OCR in parallel on the first frame of a new card.

        Vision label is used as the primary tcgapi search term when available; OCR name is the
        fallback. This eliminates the multi-tick stabilization wait.
        """
        base["lookup_stage"] = "Vision snapshot + OCR (parallel)…"
        self._emit_snapshot(dict(base))

        def ocr_task():
            return read_card(warped, name_only=True)

        def vision_task():
            print("[VISION] snapshot Web Detection on tick 1…")
            return vision.analyze(warped)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_ocr = pool.submit(ocr_task)
            fut_vis = pool.submit(vision_task)
            ocr_result = fut_ocr.result()
            vis_result = fut_vis.result()

        vision_raw = (vis_result.best_label or "").strip()
        ocr_name = (ocr_result.guessed_name or "").strip()

        # Pick the best search term: cleaned Vision label → entity fallback → OCR.
        search_name = _pick_vision_search_name(vis_result, ocr_name)

        # Always show the raw Vision label in the overlay for transparency.
        if vision_raw:
            base["vision_label"] = vision_raw
        if vis_result.tcgplayer_url:
            base["vision_tcgplayer_url"] = vis_result.tcgplayer_url
        if ocr_name:
            base["ocr_name"] = ocr_name

        if not search_name:
            base["lookup_stage"] = "No card name from Vision or OCR."
            self._emit_snapshot(base)
            return

        # Cache key uses the chosen search name so Vision-sourced results are stored separately
        # from OCR-only lookups (avoids stale cache hits when OCR and Vision disagree).
        if search_name != ocr_name:
            vision_key = normalize_price_lookup_key(search_name)
            lk = f"{lk}|v:{vision_key}"

        print(f"[VISION-FIRST] raw={vision_raw!r}  search={search_name!r}  ocr={ocr_name!r}")
        self._fetch_tcgapi(tcgapi, base, lk, now, search_name=search_name)

    def _reset_stable(self) -> None:
        self._stable_name = None
        self._stable_ticks = 0
        self._empty_ticks = 0


_STATUS_DEFAULT_MESSAGES: dict[str, str] = {
    "idle": "Press W to screenshot and scan the selected region.",
    "capturing": "Capturing screenshot and reading card…",
    "ocr_missing": "Install OCR: pip install .[ocr]",
    "error": "Unknown error.",
}

_STATUS_DOT_COLORS: dict[str, str] = {
    "ok": "#3ddc84",
    "warn": "#e0c34d",
    "idle": "#5a5a68",
    "error": "#e0744d",
}

_CONDITION_CAPTIONS: dict[str, str] = {
    "NM": "NEAR MINT",
    "LP": "LIGHTLY PLAYED",
    "MP": "MODERATELY PLAYED",
    "HP": "HEAVILY PLAYED",
    "DM": "DAMAGED",
}

_CONFIDENCE_BG: dict[str, str] = {
    "#3ddc84": "#1f3d2c",
    "#e0c34d": "#3d3520",
    "#e0744d": "#3d2820",
    "#8a8a98": "#2a2a34",
}


def _pill_style(fg: str, bg: str) -> str:
    return (
        f"color: {fg}; background: {bg}; border-radius: 9px; "
        "padding: 2px 8px; font-size: 9px; font-weight: 700;"
    )


class OverlayWindow(QWidget):
    request_repick = Signal()

    def __init__(self, settings: Settings, scan_region: Region) -> None:
        super().__init__()
        self._settings = settings
        self._scan_region = scan_region
        self._prefer_foil = False
        self._dragging = False
        self._drag_offset = QPoint()
        self._selected_condition = "NM"
        self._view_url = ""
        self._scan_count = 0

        self.setWindowTitle("Whatnot price checker")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )

        # --- Header: status dot + title + controls -------------------------
        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet(f"color: {_STATUS_DOT_COLORS['idle']}; font-size: 13px;")
        self._status_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        title = QLabel("WHATNOT PRICE CHECKER")
        title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        title.setStyleSheet("color: #e8e8e8; letter-spacing: 1px;")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._foil_btn = QPushButton("Normal")
        self._foil_btn.setFixedHeight(26)
        self._foil_btn.clicked.connect(self._toggle_foil)
        self._foil_btn.setStyleSheet(
            "background: #3a3a4a; color: #ccc; border: none; border-radius: 4px; "
            "padding: 0 8px; font-size: 11px;"
        )

        self._scan_btn = QPushButton("Scan (W)")
        self._scan_btn.setFixedHeight(26)
        self._scan_btn.clicked.connect(self.trigger_scan)
        self._scan_btn.setStyleSheet(
            "background: #2d4f7a; color: #eee; border: none; border-radius: 4px; "
            "padding: 0 8px; font-size: 11px;"
        )

        pick_btn = QPushButton("Pick Region")
        pick_btn.setFixedHeight(26)
        pick_btn.clicked.connect(self._on_repick)
        pick_btn.setStyleSheet(
            "background: #2d5a2d; color: #ccc; border: none; border-radius: 4px; "
            "padding: 0 8px; font-size: 11px;"
        )

        close_btn = QPushButton("×")
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet(
            "background: #333; color: #eee; border: none; border-radius: 4px;"
        )

        header = QHBoxLayout()
        header.addWidget(self._status_dot)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_btn)

        # Action buttons live on their own row so the title bar stays slim —
        # keeps the window narrow instead of stretching it to fit every button.
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(self._foil_btn)
        toolbar.addWidget(self._scan_btn)
        toolbar.addWidget(pick_btn)
        toolbar.addStretch(1)

        # --- Tabs (Scanner is the only implemented view; Collection is a
        # decorative placeholder for visual parity, not a real feature) -----
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(18)
        scanner_tab = QLabel("SCANNER")
        scanner_tab.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        scanner_tab.setStyleSheet(
            "color: #3ddc84; border-bottom: 2px solid #3ddc84; padding-bottom: 4px; letter-spacing: 1px;"
        )
        collection_tab = QLabel("COLLECTION")
        collection_tab.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        collection_tab.setStyleSheet("color: #4a4a56; padding-bottom: 4px; letter-spacing: 1px;")
        collection_tab.setToolTip("Coming soon")
        tabs_row.addWidget(scanner_tab)
        tabs_row.addWidget(collection_tab)
        tabs_row.addStretch(1)

        # --- Status message (idle/capturing/error/no-match) ----------------
        self._status_label = QLabel("")
        self._status_label.setFont(QFont("Segoe UI", 10))
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #b8b8c0;")
        self._status_label.setMinimumWidth(270)

        # --- Card view (shown once a card has been identified) -------------
        self._card_widget = QWidget()
        card_layout = QVBoxLayout(self._card_widget)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        self._thumb_label = QLabel()
        self._thumb_label.setFixedSize(96, 132)
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setStyleSheet(
            "background: #0c0c10; border: 1px solid #2c2c36; border-radius: 8px;"
        )
        top_row.addWidget(self._thumb_label)

        price_col = QVBoxLayout()
        price_col.setSpacing(2)
        self._condition_caption = QLabel(_CONDITION_CAPTIONS["NM"])
        self._condition_caption.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._condition_caption.setStyleSheet("color: #8a8a98; letter-spacing: 1px;")
        self._price_label = QLabel("—")
        self._price_label.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        self._price_label.setStyleSheet("color: #666;")
        self._source_caption = QLabel("")
        self._source_caption.setFont(QFont("Segoe UI", 9))
        self._source_caption.setStyleSheet("color: #6e6e7a;")
        self._change_label = QLabel("")
        self._change_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        price_col.addWidget(self._condition_caption)
        price_col.addWidget(self._price_label)
        price_col.addWidget(self._source_caption)
        price_col.addWidget(self._change_label)
        price_col.addStretch(1)
        top_row.addLayout(price_col)
        top_row.addStretch(1)
        card_layout.addLayout(top_row)

        self._name_label = QLabel("—")
        self._name_label.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._name_label.setStyleSheet("color: #f0f0f2;")
        self._name_label.setWordWrap(True)
        self._set_label = QLabel("—")
        self._set_label.setFont(QFont("Segoe UI", 10))
        self._set_label.setStyleSheet("color: #b0b0ba;")
        self._set_label.setWordWrap(True)
        self._number_label = QLabel("")
        self._number_label.setFont(QFont("Segoe UI", 9))
        self._number_label.setStyleSheet("color: #8a8a98;")
        card_layout.addWidget(self._name_label)
        card_layout.addWidget(self._set_label)
        card_layout.addWidget(self._number_label)

        tag_row = QHBoxLayout()
        tag_row.setSpacing(6)
        self._confidence_tag = QLabel("")
        self._confidence_tag.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self._tier_tag = QLabel("")
        self._tier_tag.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self._tier_tag.setStyleSheet(_pill_style("#6ab0f5", "#1c2e3d"))
        tag_row.addWidget(self._confidence_tag)
        tag_row.addWidget(self._tier_tag)
        tag_row.addStretch(1)
        card_layout.addLayout(tag_row)

        self._conditions_caption = QLabel("CONDITIONS")
        self._conditions_caption.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._conditions_caption.setStyleSheet("color: #8a8a98; letter-spacing: 1px;")
        card_layout.addWidget(self._conditions_caption)

        conditions_row = QHBoxLayout()
        conditions_row.setSpacing(4)
        self._condition_group = QButtonGroup(self)
        self._condition_group.setExclusive(True)
        self._condition_buttons: dict[str, QPushButton] = {}
        for code in CONDITION_ORDER:
            btn = QPushButton(f"{code}\n—")
            btn.setCheckable(True)
            btn.setFixedSize(50, 36)
            btn.setStyleSheet(
                "QPushButton { background: #2a2a34; color: #d0d0d0; border: 1px solid #3d3d4a; "
                "border-radius: 6px; font-size: 10px; font-weight: 600; padding: 2px; }"
                "QPushButton:checked { background: #2d6e4f; border-color: #3ddc84; color: #eafff0; }"
                "QPushButton:disabled { color: #555560; border-color: #2c2c36; }"
            )
            btn.clicked.connect(lambda _checked=False, c=code: self._on_condition_clicked(c))
            self._condition_group.addButton(btn)
            self._condition_buttons[code] = btn
            conditions_row.addWidget(btn)
        self._condition_buttons["NM"].setChecked(True)
        card_layout.addLayout(conditions_row)

        self._view_btn = QPushButton("VIEW ON TCGPLAYER  ›")
        self._view_btn.setFixedHeight(32)
        self._view_btn.setEnabled(False)
        self._view_btn.clicked.connect(self._open_tcgplayer)
        self._view_btn.setStyleSheet(
            "QPushButton { background: #1f3d2c; color: #3ddc84; border: 1px solid #2d6e4f; "
            "border-radius: 6px; font-size: 11px; font-weight: 700; } "
            "QPushButton:disabled { color: #555560; background: #232328; border-color: #2c2c36; }"
        )
        card_layout.addWidget(self._view_btn)

        self._footer_label = QLabel("")
        self._footer_label.setFont(QFont("Segoe UI", 8))
        self._footer_label.setStyleSheet("color: #5a5a68;")
        self._footer_label.setWordWrap(True)
        card_layout.addWidget(self._footer_label)

        self._card_widget.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(toolbar)
        layout.addLayout(tabs_row)
        layout.addWidget(self._status_label)
        layout.addWidget(self._card_widget)

        self.setAutoFillBackground(True)
        self.setStyleSheet(
            "OverlayWindow {"
            "  background-color: #15151a;"
            "  border: 1px solid #2c2c36;"
            "  border-radius: 10px;"
            "}"
        )
        self.resize(310, 458)
        self.setMinimumWidth(290)

        self._latest_payload: dict = {}
        self._last_digest_key: object | None = None
        self._sticky_tcgplayer_url: str = ""
        self._sticky_tcgplayer_label: str = ""
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(max(100, settings.ui_refresh_ms))
        self._ui_timer.timeout.connect(self._render_live_tick)

        self._scan_shortcut = QShortcut(QKeySequence("W"), self)
        self._scan_shortcut.activated.connect(self.trigger_scan)

        self._worker: ScanWorker | None = None
        self._render_body({"status": "idle"})

    def _toggle_foil(self) -> None:
        self._prefer_foil = not self._prefer_foil
        label = "Foil" if self._prefer_foil else "Normal"
        self._foil_btn.setText(label)
        self._foil_btn.setStyleSheet(
            f"background: {'#5a3a2d' if self._prefer_foil else '#3a3a4a'}; "
            "color: #ccc; border: none; border-radius: 4px; "
            "padding: 0 8px; font-size: 11px;"
        )
        if self._worker is not None and self._worker.isRunning():
            self._worker.set_foil_preference(self._prefer_foil)

    def _on_condition_clicked(self, code: str) -> None:
        self._selected_condition = code
        self._render_body(self._latest_payload)

    def _open_tcgplayer(self) -> None:
        if self._view_url:
            QDesktopServices.openUrl(QUrl(self._view_url))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._ui_timer.isActive():
            self._ui_timer.start()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def closeEvent(self, event) -> None:
        self._ui_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            if not self._worker.wait(3000):
                self._worker.terminate()
                self._worker.wait(1000)
        super().closeEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def update_region(self, region: Region) -> None:
        """Apply a new scan region and wait for the next explicit screenshot scan."""
        self._scan_region = region
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        self._sticky_tcgplayer_url = ""
        self._sticky_tcgplayer_label = ""
        self._worker = None
        self._last_digest_key = None
        self._latest_payload = {"status": "idle", "detail": "Region updated. Press W to screenshot and scan."}
        self._render_body(self._latest_payload)

    def trigger_scan(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._render_body({"status": "idle", "detail": "Scan already running…"})
            return
        self._selected_condition = "NM"
        self._scan_count += 1
        self._scan_btn.setEnabled(False)
        self._latest_payload = {"status": "capturing"}
        self._render_body(self._latest_payload)
        self._worker = ScanWorker(self._settings, self._scan_region, prefer_foil=self._prefer_foil, parent=self)
        self._worker.snapshot.connect(self._on_snapshot)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.start()

    def _on_scan_finished(self) -> None:
        self._scan_btn.setEnabled(True)

    def _on_repick(self) -> None:
        self.request_repick.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_W:
            self.trigger_scan()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = False

    def _digest_key(self, data: dict) -> tuple:
        """When this changes, repaint immediately (prices, catalog, OCR name identity, API errors)."""
        ocr_key = normalize_price_lookup_key(data.get("ocr_name") or "")
        err = (data.get("detail") or "") if not data.get("tcg_name") else ""
        conditions = tuple(sorted((data.get("condition_prices") or {}).items()))
        return (
            data.get("status"),
            data.get("tcg_name"),
            data.get("market"),
            data.get("low"),
            ocr_key,
            err,
            data.get("api_http_calls", 0),
            data.get("vision_label", ""),
            data.get("vision_tcgplayer_url", ""),
            conditions,
            data.get("price_source", ""),
            data.get("tcgplayer_id", ""),
            data.get("rarity", ""),
        )

    def _on_snapshot(self, data: dict) -> None:
        self._latest_payload = data
        status = data.get("status", "")
        if status != "card":
            # Clear sticky links when the overlay returns to its explicit idle state.
            if status == "idle":
                self._sticky_tcgplayer_url = ""
                self._sticky_tcgplayer_label = ""
            self._last_digest_key = None
            self._render_body(data)
            return

        # Update sticky link whenever a fresh URL arrives.
        new_url = (data.get("vision_tcgplayer_url") or "").strip()
        new_label = (data.get("tcg_name") or data.get("vision_label") or data.get("ocr_name") or "").strip()
        if new_url:
            self._sticky_tcgplayer_url = new_url
            self._sticky_tcgplayer_label = new_label
        elif new_label and new_label != self._sticky_tcgplayer_label:
            # A clearly different card has been identified without a URL — drop the old link.
            self._sticky_tcgplayer_url = ""
            self._sticky_tcgplayer_label = new_label

        key = self._digest_key(data)
        if key != self._last_digest_key:
            self._last_digest_key = key
            self._render_body(data)

    def _render_live_tick(self) -> None:
        data = self._latest_payload
        if data.get("status") == "card":
            self._render_body(data)

    @staticmethod
    def _truncate_line(s: str, max_len: int = 76) -> str:
        t = str(s).strip()
        if len(t) <= max_len:
            return t
        return t[: max_len - 1] + "…"

    def _set_status_dot(self, kind: str) -> None:
        color = _STATUS_DOT_COLORS.get(kind, _STATUS_DOT_COLORS["idle"])
        self._status_dot.setStyleSheet(f"color: {color}; font-size: 13px;")

    def _render_body(self, data: dict) -> None:
        status = data.get("status", "")
        if not status:
            return

        if status != "card":
            self._card_widget.hide()
            self._status_label.show()
            message = data.get("detail") or _STATUS_DEFAULT_MESSAGES.get(status, "Unknown status.")
            self._status_label.setText(str(message))
            self._set_status_dot("error" if status == "error" else "idle")
            return

        self._status_label.hide()
        self._card_widget.show()

        tcg_name = (data.get("tcg_name") or "").strip()
        ocr_name = (data.get("ocr_name") or "").strip()
        collector = (data.get("collector_number") or "").strip()
        card_number = (data.get("card_number") or "").strip()
        display_number = card_number or collector or ""
        rarity = (data.get("rarity") or "").strip()
        market = (data.get("market") or "").strip()

        self._set_status_dot("ok" if tcg_name else "warn")

        # Thumbnail of the actual captured/warped crop.
        frame = data.get("warp_frame")
        if frame is not None:
            try:
                self._thumb_label.setPixmap(_rounded_pixmap(_bgr_to_pixmap(frame, 92, 128), 8))
            except Exception:
                pass

        # Condition grid — real NM/LP/MP/HP/DM prices from JustTCG when available.
        conditions: dict[str, float | None] = data.get("condition_prices") or {}
        for code, btn in self._condition_buttons.items():
            price = conditions.get(code)
            btn.setText(f"{code}\n{f'${price:.2f}' if price is not None else '—'}")
            btn.setEnabled(price is not None)

        if conditions.get(self._selected_condition) is None:
            fallback = next((c for c in CONDITION_ORDER if conditions.get(c) is not None), None)
            if fallback:
                self._selected_condition = fallback
                self._condition_buttons[fallback].setChecked(True)

        selected_price = conditions.get(self._selected_condition)
        if selected_price is not None:
            price_text = f"${selected_price:.2f}"
        elif market and market != "—":
            price_text = market if market.startswith("$") else f"${market}"
        else:
            price_text = "—"
        self._price_label.setText(price_text)
        self._price_label.setStyleSheet(f"color: {'#3ddc84' if price_text != '—' else '#666'};")
        self._condition_caption.setText(
            _CONDITION_CAPTIONS.get(self._selected_condition, "NEAR MINT")
        )

        source = (data.get("price_source") or "").strip()
        lookup_ms = data.get("lookup_ms") or 0
        if source:
            self._source_caption.setText(f"via {source} · {lookup_ms}ms")
            self._source_caption.show()
        else:
            self._source_caption.hide()

        change = data.get("price_change_24h")
        if isinstance(change, (int, float)):
            if change > 0:
                arrow, color = "▲", "#3ddc84"
            elif change < 0:
                arrow, color = "▼", "#e0744d"
            else:
                arrow, color = "■", "#8a8a98"
            self._change_label.setText(f"{arrow} {abs(change):.1f}% 24h")
            self._change_label.setStyleSheet(f"color: {color};")
            self._change_label.show()
        else:
            self._change_label.hide()

        self._name_label.setText(tcg_name or ocr_name or "—")
        self._set_label.setText((data.get("set_name") or "").strip() or "—")
        bits = []
        if display_number:
            bits.append(f"#{display_number}")
        if rarity:
            bits.append(rarity)
        self._number_label.setText(" · ".join(bits))

        conf_label, conf_color = _confidence_label(data)
        self._confidence_tag.setText(conf_label)
        self._confidence_tag.setStyleSheet(
            _pill_style(conf_color, _CONFIDENCE_BG.get(conf_color, "#2a2a34"))
        )

        tier_label = _price_tier_label(market)
        if tier_label:
            self._tier_tag.setText(f"● {tier_label}")
            self._tier_tag.show()
        else:
            self._tier_tag.hide()

        printing = (data.get("printing") or data.get("foil_guess") or "Normal").strip()
        self._conditions_caption.setText(f"CONDITIONS · {printing.upper()}")

        tcgplayer_id = (data.get("tcgplayer_id") or "").strip()
        url = (data.get("vision_tcgplayer_url") or "").strip() or self._sticky_tcgplayer_url
        if not url and tcgplayer_id:
            url = f"https://www.tcgplayer.com/product/{tcgplayer_id}"
        self._view_url = url
        self._view_btn.setEnabled(bool(url))

        detail = (data.get("detail") or "").strip()
        stage = (data.get("lookup_stage") or "").strip()
        note = detail or (stage if not tcg_name else "")
        if not note and not source and not self._settings.justtcg_api_key:
            note = "Set JUSTTCG_API_KEY for LP/MP/HP/DM prices"
        scans = self._scan_count
        tier = "JUSTTCG" if self._settings.justtcg_api_key else "BASIC"
        status_line = f"{scans} SCAN{'S' if scans != 1 else ''} THIS SESSION  ·  {tier}  ·  v{__version__}"
        self._footer_label.setText(self._truncate_line(note, 56) if note else status_line)


def run_app() -> int:
    from whatnot_price_checker.region_picker import RegionPicker

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    settings = Settings.from_env()

    # Without macOS Screen Recording permission, every grab silently returns
    # the desktop wallpaper instead of the browser window underneath — no
    # error, no exception, it just looks like the app is "screenshotting the
    # desktop". Catch that here instead of leaving the user to guess why.
    if has_screen_recording_access() is False:
        request_screen_recording_access()
        QMessageBox.warning(
            None,
            "Screen Recording permission needed",
            "macOS is blocking this app from seeing your browser window.\n\n"
            "Without Screen Recording permission, every scan just captures your "
            "desktop wallpaper instead of the card on screen — that's why the "
            "thumbnail looks wrong.\n\n"
            "Fix: open System Settings -> Privacy & Security -> Screen Recording, "
            "enable the terminal/app you used to launch this program, then fully "
            "quit and relaunch it (just checking the box isn't enough — the app "
            "has to restart).",
        )

    overlay: OverlayWindow | None = None
    picker_ref: RegionPicker | None = None

    # Reuse the last-picked region automatically if it still fits the current
    # monitor layout, so the picker only needs to run once per machine setup.
    scan_region: Region | None = load_region()

    def _open_overlay() -> None:
        nonlocal overlay
        if scan_region is None:
            return
        if overlay is not None:
            overlay.update_region(scan_region)
            overlay.show()
            overlay.raise_()
            return
        overlay = OverlayWindow(settings, scan_region)
        overlay.request_repick.connect(_show_picker)
        overlay.show()

    def _on_region(left: int, top: int, width: int, height: int) -> None:
        nonlocal scan_region
        scan_region = Region(left=left, top=top, width=width, height=height)
        save_region(scan_region)
        if overlay is not None:
            overlay.update_region(scan_region)
            overlay.show()
            overlay.raise_()
        else:
            _open_overlay()

    def _on_cancel() -> None:
        if overlay is not None:
            overlay.show()
            overlay.raise_()
        else:
            _open_overlay()

    def _show_picker() -> None:
        nonlocal picker_ref
        if overlay is not None:
            overlay.hide()
        picker_ref = RegionPicker()
        picker_ref.region_selected.connect(_on_region)
        picker_ref.cancelled.connect(_on_cancel)
        picker_ref.show()

    if scan_region is not None:
        _open_overlay()
    else:
        _show_picker()
    return app.exec()
