from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from whatnot_price_checker.capture import Region, grab_region
from whatnot_price_checker.card import warp_for_ocr
from whatnot_price_checker.config import Settings, normalize_price_lookup_key
from whatnot_price_checker.ocr_reader import read_card
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

        try:
            try:
                self._tick(None, tcgplayer, vision)
            except Exception as e:
                self._emit_snapshot({"status": "error", "detail": str(e)})
        finally:
            if tcgplayer is not None:
                tcgplayer.close()
            if vision is not None:
                vision.close()

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
    ) -> None:
        if self._stop or self.isInterruptionRequested():
            return
        frame = grab_region(self._scan_region)
        warped, warp_source = warp_for_ocr(frame)
        if warped is None:
            self._reset_stable()
            self._emit_snapshot({"status": "idle", "detail": "No card-shaped region found in screenshot. Press S to try again."})
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

        if tcgplayer is None:
            base["detail"] = (
                "Set TCGPLAYER_ACCESS_TOKEN or TCGPLAYER_PUBLIC_KEY/TCGPLAYER_PRIVATE_KEY for prices."
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

        self._fetch_tcgplayer(tcgplayer, base, lk, now, search_name=search_name)

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
        client: TcgClient,
        base: dict,
        lk: str,
        now: float,
        *,
        search_name: str | None = None,
    ) -> None:
        self._api_http_calls += 1
        name = (search_name or base["ocr_name"]).strip() or base["ocr_name"]
        collector = (base.get("collector_number") or "").strip()
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
            return

        if quote is None:
            base["detail"] = "No TCGPlayer catalog match."
            base["lookup_stage"] = "No catalog match (call still counted)."
            self._emit_snapshot(base)
            return

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
        self._store_cache(lk, now, base, bundle)

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


class OverlayWindow(QWidget):
    request_repick = Signal()

    def __init__(self, settings: Settings, scan_region: Region) -> None:
        super().__init__()
        self._settings = settings
        self._scan_region = scan_region
        self._prefer_foil = False
        self._dragging = False
        self._drag_offset = QPoint()

        if (
            settings.tcgplayer_access_token
            or (
                settings.tcgplayer_public_key and settings.tcgplayer_private_key
            )
        ):
            subtitle = "Whatnot · TCGPlayer API"
        else:
            subtitle = "Whatnot · price checker"

        self.setWindowTitle("Whatnot price checker")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )

        title = QLabel(subtitle)
        title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        title.setStyleSheet("color: #e8e8e8;")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._body = QLabel("")
        self._body.setFont(QFont("Consolas", 10))
        self._body.setWordWrap(True)
        self._body.setStyleSheet("color: #d0d0d0;")
        self._body.setMinimumWidth(320)
        self._body.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._link_label = QLabel("")
        self._link_label.setFont(QFont("Consolas", 10))
        self._link_label.setWordWrap(True)
        self._link_label.setStyleSheet("color: #6ab0f5;")
        self._link_label.setTextFormat(Qt.TextFormat.RichText)
        self._link_label.setOpenExternalLinks(True)
        self._link_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self._link_label.setMinimumWidth(320)
        self._link_label.hide()

        self._foil_btn = QPushButton("Normal")
        self._foil_btn.setFixedHeight(28)
        self._foil_btn.clicked.connect(self._toggle_foil)
        self._foil_btn.setStyleSheet(
            "background: #3a3a4a; color: #ccc; border: none; border-radius: 4px; "
            "padding: 0 8px; font-size: 11px;"
        )

        self._scan_btn = QPushButton("Scan (S)")
        self._scan_btn.setFixedHeight(28)
        self._scan_btn.clicked.connect(self.trigger_scan)
        self._scan_btn.setStyleSheet(
            "background: #2d4f7a; color: #eee; border: none; border-radius: 4px; "
            "padding: 0 8px; font-size: 11px;"
        )

        pick_btn = QPushButton("Pick Region")
        pick_btn.setFixedHeight(28)
        pick_btn.clicked.connect(self._on_repick)
        pick_btn.setStyleSheet(
            "background: #2d5a2d; color: #ccc; border: none; border-radius: 4px; "
            "padding: 0 8px; font-size: 11px;"
        )

        close_btn = QPushButton("×")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet(
            "background: #333; color: #eee; border: none; border-radius: 4px;"
        )

        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._foil_btn)
        header.addWidget(self._scan_btn)
        header.addWidget(pick_btn)
        header.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self._body)
        layout.addWidget(self._link_label)

        self.setAutoFillBackground(True)
        self.setStyleSheet(
            "OverlayWindow {"
            "  background-color: #1e1e24;"
            "  border: 1px solid #3d3d4a;"
            "  border-radius: 10px;"
            "}"
        )
        self.resize(360, 168)

        self._latest_payload: dict = {}
        self._last_digest_key: object | None = None
        self._sticky_tcgplayer_url: str = ""
        self._sticky_tcgplayer_label: str = ""
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(max(100, settings.ui_refresh_ms))
        self._ui_timer.timeout.connect(self._render_live_tick)

        self._scan_shortcut = QShortcut(QKeySequence("S"), self)
        self._scan_shortcut.activated.connect(self.trigger_scan)

        self._worker: ScanWorker | None = None
        self._render_body({"status": "idle", "detail": "Press S to screenshot and scan the selected region."})

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
        self._latest_payload = {"status": "idle", "detail": "Region updated. Press S to screenshot and scan."}
        self._render_body(self._latest_payload)

    def trigger_scan(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._body.setText("Scan already running…")
            return
        self._scan_btn.setEnabled(False)
        self._latest_payload = {"status": "capturing", "detail": "Capturing screenshot and reading card…"}
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
        if event.key() == Qt.Key.Key_S:
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

    def _render_body(self, data: dict) -> None:
        status = data.get("status", "")
        if not status:
            return
        if status == "idle":
            detail = data.get("detail") or "Press S to screenshot and scan the selected region."
            self._body.setText(str(detail))
            self._link_label.hide()
            return
        if status == "capturing":
            self._body.setText(data.get("detail", "Capturing screenshot…"))
            self._link_label.hide()
            return
        if status == "ocr_missing":
            self._body.setText(data.get("detail", "Install OCR: pip install .[ocr]"))
            self._link_label.hide()
            return
        if status == "error":
            self._body.setText(data.get("detail", "Unknown error"))
            self._link_label.hide()
            return

        tcg_name = (data.get("tcg_name") or "").strip()
        ocr_name = (data.get("ocr_name") or "").strip()
        collector = (data.get("collector_number") or "").strip()
        card_number = (data.get("card_number") or "").strip()
        display_number = card_number or collector or ""
        display_name = tcg_name or ocr_name or "—"
        if display_number:
            display_name = f"{display_name}  ({display_number})"
        display_set = (data.get("set_name") or "").strip() or "—"
        market = (data.get("market") or "").strip() or "—"
        if market != "—" and not market.startswith("$"):
            market = f"${market}"

        foil_pref = (data.get("foil_guess") or "Normal").strip()
        listing_print = (data.get("printing") or "").strip()
        if listing_print:
            foil_line = f"Printing: {foil_pref} · {listing_print} (listing)"
        else:
            foil_line = f"Printing: {foil_pref}"

        lines = [
            f"Name: {display_name}",
            f"Set: {display_set}",
            foil_line,
            f"NM market: {market}",
        ]

        vision_label = (data.get("vision_label") or "").strip()
        if vision_label:
            lines.append(f"Vision: {self._truncate_line(vision_label, 60)}  ✓")

        stage = (data.get("lookup_stage") or "").strip()
        if stage and not tcg_name:
            lines.append(stage)
        if data.get("detail"):
            lines.append(self._truncate_line(str(data["detail"]), 72))

        ocr_raw = (data.get("ocr_lines") or "").strip()
        if ocr_raw and not tcg_name and not vision_label:
            lines.append(f"OCR: {self._truncate_line(ocr_raw, 64)}")

        self._body.setText("\n".join(lines))

        # Use current snapshot URL when available; fall back to sticky URL from last good result.
        tcgplayer_url = (data.get("vision_tcgplayer_url") or "").strip() or self._sticky_tcgplayer_url
        if tcgplayer_url:
            short_url = tcgplayer_url
            if len(short_url) > 60:
                short_url = short_url[:57] + "…"
            self._link_label.setText(
                f'TCGPlayer: <a href="{tcgplayer_url}" style="color:#6ab0f5;">'
                f"{short_url}</a>"
            )
            self._link_label.show()
        else:
            self._link_label.hide()


def run_app() -> int:
    from whatnot_price_checker.region_picker import RegionPicker

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    settings = Settings.from_env()

    overlay: OverlayWindow | None = None
    picker_ref: RegionPicker | None = None

    scan_region: Region | None = None

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

    _show_picker()
    return app.exec()
