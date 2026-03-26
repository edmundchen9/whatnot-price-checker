from __future__ import annotations

import sys
import time

from PySide6.QtCore import QPoint, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from whatnot_price_checker.capture import grab_region, primary_monitor_region, region_from_window
from whatnot_price_checker.card import find_card_warp
from whatnot_price_checker.config import Settings, normalize_price_lookup_key
from whatnot_price_checker.foil import foil_art_texture_ratio, foil_likely
from whatnot_price_checker.ocr_reader import read_card_name
from whatnot_price_checker.tcgapi_client import TcgApiClient
from whatnot_price_checker.tcgplayer import TcgClient
from whatnot_price_checker.win_window import find_window_rect


class ScanWorker(QThread):
    snapshot = Signal(dict)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._stop = False
        self._stable_name: str | None = None
        self._stable_ticks = 0
        self._foil_last = False
        self._foil_agree_ticks = 0
        self._cache_key: str | None = None
        self._cache_until = 0.0
        self._cache_bundle: dict = {}

    def stop(self) -> None:
        self._stop = True

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
            while not self._stop:
                try:
                    self._tick(tcgapi, tcgplayer)
                except Exception as e:
                    self.snapshot.emit({"status": "error", "detail": str(e)})
                self.msleep(interval_ms)
        finally:
            if tcgapi is not None:
                tcgapi.close()
            if tcgplayer is not None:
                tcgplayer.close()

    def _tick(self, tcgapi: TcgApiClient | None, tcgplayer: TcgClient | None) -> None:
        rect = find_window_rect(self._settings.window_title_contains)
        region = region_from_window(rect) if rect is not None else primary_monitor_region()
        frame = grab_region(region)
        warped = find_card_warp(
            frame,
            min_area_ratio=self._settings.min_card_area_ratio,
            max_area_ratio=self._settings.max_card_area_ratio,
        )
        if warped is None:
            self._reset_stable()
            self.snapshot.emit({"status": "scanning"})
            return

        try:
            ocr = read_card_name(warped)
        except RuntimeError as e:
            self.snapshot.emit({"status": "ocr_missing", "detail": str(e)})
            return

        name = ocr.guessed_name.strip()
        ratio = foil_art_texture_ratio(warped)
        foil_now = foil_likely(
            warped, ratio_threshold=self._settings.foil_ratio_threshold
        )

        lines_preview = " | ".join(ocr.raw_lines[:4])
        base: dict = {
            "status": "card",
            "ocr_name": name,
            "script": ocr.script,
            "ocr_lines": lines_preview,
            "foil_ratio": round(ratio, 2),
            "foil_guess": "foil" if foil_now else "non-foil",
            "tcg_name": "",
            "set_name": "",
            "printing": "",
            "market": "",
            "low": "",
            "median": "",
            "rate_remaining": "",
            "rate_reset": "",
            "detail": "",
        }

        if not name:
            self._reset_stable()
            self.snapshot.emit(base)
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
            self.snapshot.emit(base)
            return

        if self._stable_ticks < 2 or self._foil_agree_ticks < 2:
            self.snapshot.emit(base)
            return

        lk = f"{normalize_price_lookup_key(name)}|{int(self._foil_last)}"
        now = time.time()
        if lk == self._cache_key and now < self._cache_until:
            self.snapshot.emit({**base, **self._cache_bundle})
            return

        if tcgapi is not None:
            self._fetch_tcgapi(tcgapi, base, lk, now)
        elif tcgplayer is not None:
            self._fetch_tcgplayer(tcgplayer, base, lk, now)

    def _store_cache(self, key: str, now: float, base: dict, bundle: dict) -> None:
        self._cache_key = key
        self._cache_until = now + self._settings.price_cache_ttl_sec
        self._cache_bundle = bundle
        self.snapshot.emit({**base, **bundle})

    def _fetch_tcgapi(self, client: TcgApiClient, base: dict, lk: str, now: float) -> None:
        try:
            quote, _meta = client.quote_from_search(
                base["ocr_name"],
                prefer_foil=self._foil_last,
                per_page=self._settings.tcgapi_per_page,
            )
        except Exception as e:
            base["detail"] = str(e)
            self.snapshot.emit(base)
            return

        if quote is None:
            base["detail"] = "No tcgapi.dev results for this search."
            self.snapshot.emit(base)
            return

        bundle = {
            "tcg_name": quote.name,
            "set_name": quote.set_name,
            "printing": quote.printing,
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
        try:
            quote = client.quote_best_match(base["ocr_name"])
        except Exception as e:
            base["detail"] = str(e)
            self.snapshot.emit(base)
            return

        if quote is None:
            base["detail"] = "No TCGPlayer catalog match."
            self.snapshot.emit(base)
            return

        bundle = {
            "tcg_name": quote.name,
            "set_name": "",
            "printing": "Normal",
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
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        title = QLabel(subtitle)
        title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        title.setStyleSheet("color: #e8e8e8;")

        self._body = QLabel("")
        self._body.setFont(QFont("Consolas", 10))
        self._body.setWordWrap(True)
        self._body.setStyleSheet("color: #d0d0d0;")
        self._body.setMinimumWidth(320)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet(
            "background: #333; color: #eee; border: none; border-radius: 4px;"
        )

        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._body)

        self.setStyleSheet(
            "OverlayWindow { background-color: rgba(20, 20, 24, 220); border-radius: 10px; }"
        )
        self.resize(420, 260)

        self._worker = ScanWorker(settings, self)
        self._worker.snapshot.connect(self._on_snapshot)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._worker.isRunning():
            self._worker.start()

    def closeEvent(self, event) -> None:
        self._worker.stop()
        self._worker.wait(5000)
        super().closeEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = False

    def _on_snapshot(self, data: dict) -> None:
        status = data.get("status", "")
        if status == "scanning":
            self._body.setText("Looking for a card-shaped region…")
            return
        if status == "ocr_missing":
            self._body.setText(data.get("detail", "Install OCR: pip install .[ocr]"))
            return
        if status == "error":
            self._body.setText(data.get("detail", "Unknown error"))
            return

        lines = [
            f"OCR name: {data.get('ocr_name', '')}",
            f"Language hint: {data.get('script', '')}",
            f"Foil guess: {data.get('foil_guess', '')} (texture ratio {data.get('foil_ratio', '')})",
            f"OCR lines: {data.get('ocr_lines', '')}",
        ]
        if data.get("tcg_name"):
            lines.append(f"Match: {data.get('tcg_name')}")
        if data.get("set_name"):
            lines.append(f"Set: {data.get('set_name')}")
        if data.get("printing"):
            lines.append(f"Printing: {data.get('printing')}")
        if data.get("market") or data.get("low"):
            med = data.get("median", "")
            med_part = f"   Median: {med}" if med else ""
            lines.append(
                f"Market: {data.get('market', '—')}   Low: {data.get('low', '—')}{med_part}"
            )
        if data.get("rate_remaining"):
            lines.append(
                f"tcgapi quota remaining today: {data.get('rate_remaining', '')}"
            )
        if data.get("rate_reset"):
            lines.append(f"Quota resets: {data.get('rate_reset', '')}")
        if data.get("detail"):
            lines.append(data["detail"])
        self._body.setText("\n".join(lines))


def run_app() -> int:
    app = QApplication(sys.argv)
    win = OverlayWindow(Settings.from_env())
    win.show()
    return app.exec()
