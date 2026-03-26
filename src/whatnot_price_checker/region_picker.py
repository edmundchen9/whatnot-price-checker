"""
Fullscreen overlay spanning ALL monitors that lets the user draw a rectangle.
Emits absolute screen pixel coordinates (left, top, width, height).
"""
from __future__ import annotations

import mss
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QPushButton, QWidget


def _grab_all_monitors() -> tuple[QPixmap, dict]:
    """Grab the combined virtual screen (all monitors) as a QPixmap."""
    with mss.mss() as sct:
        mon = sct.monitors[0]  # 0 = combined bounding box of all monitors
        shot = sct.grab(mon)
        bgra = np.asarray(shot, dtype=np.uint8)
        rgb = bgra[:, :, 2::-1].copy()
        h, w = rgb.shape[:2]
        img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(img), mon


class RegionPicker(QWidget):
    # Emits (left, top, width, height) in absolute screen pixels
    region_selected = Signal(int, int, int, int)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()

        self._screenshot, mon = _grab_all_monitors()
        self._virt_left = int(mon["left"])
        self._virt_top = int(mon["top"])

        self._draw_start: QPoint | None = None
        self._rect = QRect()

        self.setWindowTitle("Select scan region")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(
            self._virt_left, self._virt_top,
            self._screenshot.width(), self._screenshot.height(),
        )

        self._btn = QPushButton("Start Scanning", self)
        self._btn.setFixedSize(180, 44)
        self._btn.hide()
        self._btn.clicked.connect(self._on_confirm)
        self._btn.setStyleSheet(
            "background: #4CAF50; color: white; font-size: 14px; "
            "border: none; border-radius: 8px; font-weight: bold;"
        )

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.drawPixmap(0, 0, self._screenshot)
        p.fillRect(self.rect(), QColor(0, 0, 0, 140))

        if not self._rect.isNull() and self._rect.width() > 10:
            p.drawPixmap(self._rect, self._screenshot, self._rect)
            pen = QPen(QColor(0, 220, 80), 3)
            p.setPen(pen)
            p.drawRect(self._rect)

            p.setPen(QColor(220, 220, 220))
            p.setFont(QFont("Segoe UI", 10))
            dims = f"{self._rect.width()} x {self._rect.height()} px"
            p.drawText(self._rect.left() + 6, self._rect.top() - 10, dims)

        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        p.drawText(
            self.rect().adjusted(0, 50, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Draw a rectangle over the video area to scan",
        )
        p.setFont(QFont("Segoe UI", 13))
        p.drawText(
            self.rect().adjusted(0, 95, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Click and drag \u2014 then press Start.   ESC to cancel.",
        )
        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._draw_start = event.position().toPoint()
            self._rect = QRect()
            self._btn.hide()
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._draw_start is not None:
            end = event.position().toPoint()
            self._rect = QRect(self._draw_start, end).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._draw_start = None
        if self._rect.width() > 50 and self._rect.height() > 50:
            bx = self._rect.center().x() - 90
            by = min(self._rect.bottom() + 16, self.height() - 60)
            self._btn.move(bx, by)
            self._btn.show()
        self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()

    def _on_confirm(self) -> None:
        abs_left = self._rect.left() + self._virt_left
        abs_top = self._rect.top() + self._virt_top
        w = self._rect.width()
        h = self._rect.height()
        print(f"[PICKER] Scan region: left={abs_left}  top={abs_top}  {w}x{h}")
        self.region_selected.emit(abs_left, abs_top, w, h)
        self.close()
