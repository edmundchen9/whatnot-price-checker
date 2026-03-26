from __future__ import annotations

import sys
import time

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from whatnot_price_checker.capture import Region, grab_region
from whatnot_price_checker.card import warp_for_ocr
from whatnot_price_checker.config import Settings, normalize_price_lookup_key
from whatnot_price_checker.foil import foil_art_texture_ratio, foil_likely
from whatnot_price_checker.ocr_reader import read_card
from whatnot_price_checker.tcgapi_client import TcgApiClient
from whatnot_price_checker.tcgplayer import TcgClient


class ScanWorker(QThread):
    snapshot = Signal(dict)

    def __init__(self, settings: Settings, scan_region: Region, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._scan_region = scan_region
        self._stop = False
        self._stable_name: str | None = None
        self._stable_ticks = 0
        self._foil_last = False
        self._foil_agree_ticks = 0
        self._cache_key: str | None = None
        self._cache_until = 0.0
        self._cache_bundle: dict = {}
        self._api_http_calls = 0

    def stop(self) -> None:
        self._stop = True
        self.requestInterruption()

    def run(self) -> None:
        tcgapi: TcgApiClient | None = None
        if self._settings.tcgapi_key:
            tcgapi = TcgApiClient(self._settings.tcgapi_key)

        tcgplayer: TcgClient | None = None
        if tcgapi is None:
            has_token = bool(self._settings.tcgplayer_access_token)
            has_keys = bool(
                self._settings.tcgplayer_public_key and self._settings.tcgplayer_private_key
            )
            if has_token or has_keys:
                tcgplayer = TcgClient(self._settings)

        interval_ms = max(50, int(1000.0 / max(0.5, self._settings.capture_fps)))

        try:
            while not self._stop and not self.isInterruptionRequested():
                try:
                    self._tick(tcgapi, tcgplayer)
                except Exception as e:
                    self._emit_snapshot({"status": "error", "detail": str(e)})
                self._sleep_cancellable(interval_ms)
        finally:
            if tcgapi is not None:
                tcgapi.close()
            if tcgplayer is not None:
                tcgplayer.close()

    def _sleep_cancellable(self, total_ms: int) -> None:
        remaining = total_ms
        while remaining > 0 and not self._stop and not self.isInterruptionRequested():
            chunk = min(50, remaining)
            self.msleep(chunk)
            remaining -= chunk

    def _tick(self, tcgapi: TcgApiClient | None, tcgplayer: TcgClient | None) -> None:
        if self._stop or self.isInterruptionRequested():
            return
        frame = grab_region(self._scan_region)
        warped, warp_source = warp_for_ocr(frame)
        if warped is None:
            self._reset_stable()
            self._emit_snapshot({"status": "scanning"})
            return

        try:
            ocr = read_card(warped)
        except RuntimeError as e:
            self._emit_snapshot({"status": "ocr_missing", "detail": str(e)})
            return

        name = ocr.guessed_name.strip()
        collector = ocr.collector_number.strip()

        ratio = foil_art_texture_ratio(warped)
        foil_now = foil_likely(
            warped, ratio_threshold=self._settings.foil_ratio_threshold
        )

        lines_preview = " | ".join(ocr.raw_lines[:6])
        print(f"[OCR] warp={warp_source}  name={name!r}  collector={collector!r}  "
              f"foil={'Y' if foil_now else 'N'}  raw={lines_preview}")

        base: dict = {
            "status": "card",
            "warp_source": warp_source,
            "ocr_name": name,
            "collector_number": collector,
            "script": ocr.script,
            "ocr_lines": lines_preview,
            "foil_ratio": round(ratio, 2),
            "foil_guess": "foil" if foil_now else "non-foil",
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
        }

        if not name:
            self._reset_stable()
            base["lookup_stage"] = "No card name detected by OCR."
            print("[SCAN] No name detected, skipping API lookup.")
            self._emit_snapshot(base)
            return

        if name != self._stable_name:
            self._stable_name = name
            self._stable_ticks = 1
            self._foil_last = foil_now
            self._foil_agree_ticks = 1
        else:
            self._stable_ticks += 1
            if foil_now == self._foil_last:
                self._foil_agree_ticks += 1
            else:
                self._foil_last = foil_now
                self._foil_agree_ticks = 1

        if tcgapi is None and tcgplayer is None:
            base["detail"] = "Set TCGAPI_KEY (tcgapi.dev) or TCGPlayer API env vars for prices."
            base["lookup_stage"] = "No price API key configured."
            self._emit_snapshot(base)
            return

        if self._stable_ticks < 2 or self._foil_agree_ticks < 2:
            base["lookup_stage"] = (
                f"Stabilizing: name {self._stable_ticks}/2, foil {self._foil_agree_ticks}/2"
            )
            self._emit_snapshot(base)
            return

        lk = f"{normalize_price_lookup_key(name)}|{int(self._foil_last)}"
        now = time.time()
        if lk == self._cache_key and now < self._cache_until:
            base["lookup_stage"] = "Cached"
            self._emit_snapshot({**base, **self._cache_bundle})
            return

        if tcgapi is not None:
            self._fetch_tcgapi(tcgapi, base, lk, now)
        elif tcgplayer is not None:
            self._fetch_tcgplayer(tcgplayer, base, lk, now)

    def _emit_snapshot(self, payload: dict) -> None:
        out = dict(payload)
        out["api_http_calls"] = self._api_http_calls
        out["has_tcgapi_key"] = bool(self._settings.tcgapi_key)
        self.snapshot.emit(out)

    def _store_cache(self, key: str, now: float, base: dict, bundle: dict) -> None:
        self._cache_key = key
        self._cache_until = now + self._settings.price_cache_ttl_sec
        bundle = {**bundle, "lookup_stage": "Last lookup succeeded; prices cached for this card."}
        self._cache_bundle = bundle
        self._emit_snapshot({**base, **bundle})

    def _fetch_tcgapi(self, client: TcgApiClient, base: dict, lk: str, now: float) -> None:
        self._api_http_calls += 1
        search_q = base["ocr_name"]
        collector = base.get("collector_number", "")
        if collector:
            search_q = f"{search_q} {collector}"
        print(f"[API] tcgapi.dev search: {search_q!r}  (call #{self._api_http_calls})")
        base["lookup_stage"] = f"Searching: {search_q!r} …"
        self._emit_snapshot(dict(base))
        try:
            quote, _meta = client.quote_from_search(
                search_q,
                prefer_foil=self._foil_last,
                per_page=self._settings.tcgapi_per_page,
            )
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
            want = "Foil" if self._foil_last else "Normal"
            bundle["detail"] = (
                f"No {want} row in top results; showing closest listing ({quote.printing})."
            )
        self._store_cache(lk, now, base, bundle)

    def _fetch_tcgplayer(self, client: TcgClient, base: dict, lk: str, now: float) -> None:
        self._api_http_calls += 1
        base["lookup_stage"] = f"TCGPlayer API request (session #{self._api_http_calls})…"
        self._emit_snapshot(dict(base))
        try:
            quote = client.quote_best_match(base["ocr_name"])
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
            "set_name": "",
            "printing": "Normal",
            "rarity": "",
            "card_number": "",
            "product_type": "",
            "catalog_foil_only": "",
            "listings": "",
            "market": f"{quote.market_price:.2f}" if quote.market_price is not None else "—",
            "low": f"{quote.low_price:.2f}" if quote.low_price is not None else "—",
            "median": "",
            "rate_remaining": "",
            "rate_reset": "",
            "detail": "",
        }
        self._store_cache(lk, now, base, bundle)

    def _reset_stable(self) -> None:
        self._stable_name = None
        self._stable_ticks = 0
        self._foil_last = False
        self._foil_agree_ticks = 0


class OverlayWindow(QWidget):
    request_repick = Signal()

    def __init__(self, settings: Settings, scan_region: Region) -> None:
        super().__init__()
        self._settings = settings
        self._scan_region = scan_region
        self._dragging = False
        self._drag_offset = QPoint()

        if settings.tcgapi_key:
            subtitle = "Whatnot · tcgapi.dev"
        elif (
            settings.tcgplayer_access_token
            or (
                settings.tcgplayer_public_key and settings.tcgplayer_private_key
            )
        ):
            subtitle = "Whatnot · TCGPlayer API"
        else:
            subtitle = "Whatnot · price checker"

        self.setWindowTitle("Whatnot price checker")
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
        header.addWidget(pick_btn)
        header.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self._body)

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
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(max(100, settings.ui_refresh_ms))
        self._ui_timer.timeout.connect(self._render_live_tick)

        self._worker = ScanWorker(settings, self._scan_region, self)
        self._worker.snapshot.connect(self._on_snapshot)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._ui_timer.isActive():
            self._ui_timer.start()
        if not self._worker.isRunning():
            self._worker.start()

    def closeEvent(self, event) -> None:
        self._ui_timer.stop()
        self._worker.stop()
        if not self._worker.wait(3000):
            self._worker.terminate()
            self._worker.wait(1000)
        super().closeEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def update_region(self, region: Region) -> None:
        """Apply a new scan region and restart the scanner."""
        self._scan_region = region
        if self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        self._worker = ScanWorker(self._settings, self._scan_region, self)
        self._worker.snapshot.connect(self._on_snapshot)
        self._worker.start()
        self._body.setText("Region updated — scanning…")

    def _on_repick(self) -> None:
        self.request_repick.emit()

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
        )

    def _on_snapshot(self, data: dict) -> None:
        self._latest_payload = data
        status = data.get("status", "")
        if status != "card":
            self._last_digest_key = None
            self._render_body(data)
            return

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
        if status == "scanning":
            self._body.setText("Scanning for card…")
            return
        if status == "ocr_missing":
            self._body.setText(data.get("detail", "Install OCR: pip install .[ocr]"))
            return
        if status == "error":
            self._body.setText(data.get("detail", "Unknown error"))
            return

        tcg_name = (data.get("tcg_name") or "").strip()
        ocr_name = (data.get("ocr_name") or "").strip()
        display_name = tcg_name or ocr_name or "—"
        display_set = (data.get("set_name") or "").strip() or "—"
        market = (data.get("market") or "").strip() or "—"
        if market != "—" and not market.startswith("$"):
            market = f"${market}"

        scan_foil = (data.get("foil_guess") or "—").strip()
        listing_print = (data.get("printing") or "").strip()
        if listing_print:
            foil_line = f"Foil: {scan_foil} (video) · {listing_print} (listing)"
        else:
            foil_line = f"Foil: {scan_foil} (video)"

        lines = [
            f"Name: {display_name}",
            f"Set: {display_set}",
            foil_line,
            f"NM market: {market}",
        ]

        stage = (data.get("lookup_stage") or "").strip()
        if stage and not tcg_name:
            lines.append(stage)
        if data.get("detail"):
            lines.append(self._truncate_line(str(data["detail"]), 72))

        ocr_raw = (data.get("ocr_lines") or "").strip()
        if ocr_raw and not tcg_name:
            lines.append(f"OCR: {self._truncate_line(ocr_raw, 64)}")

        self._body.setText("\n".join(lines))


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
