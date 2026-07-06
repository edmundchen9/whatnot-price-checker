"""
Fullscreen overlay spanning ALL monitors that lets the user draw a rectangle.
Emits absolute screen coordinates (left, top, width, height) in the same
logical-point units as ``mss.monitors`` / Qt widget geometry — NOT raw
physical pixels (those can differ by the display's device pixel ratio on
HiDPI/Retina screens). ``capture.py``'s ``grab_region`` passes these straight
to ``mss.grab()``, which expects that same logical-point coordinate system.
"""
from __future__ import annotations

import mss
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QPushButton, QWidget


def _grab_all_monitors() -> tuple[QPixmap, dict]:
    """Grab the combined virtual screen (all monitors) as a QPixmap.

    On HiDPI/Retina displays, ``mss``'s ``monitors`` bounds are reported in
    *logical points* (matching Qt's coordinate system for widgets/mouse
    events), but ``sct.grab()`` returns the image at *physical pixel*
    resolution (e.g. 2x on Retina). The returned pixmap's device pixel ratio
    is set accordingly so it draws at the correct logical size — without
    this, the picker window would be sized in physical pixels while Qt treats
    that as logical points, making it ~2x too large and causing the drawn
    selection rectangle to land on the wrong part of the screen.
    """
    with mss.mss() as sct:
        mon = sct.monitors[0]  # 0 = combined bounding box of all monitors, in logical points
        shot = sct.grab(mon)
        bgra = np.asarray(shot, dtype=np.uint8)
        rgb = bgra[:, :, 2::-1].copy()
        h, w = rgb.shape[:2]
        img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
    pixmap = QPixmap.fromImage(img)
    mon_w, mon_h = int(mon["width"]), int(mon["height"])
    if mon_w > 0 and mon_h > 0:
        dpr = max(pixmap.width() / mon_w, pixmap.height() / mon_h)
        if dpr > 0:
            pixmap.setDevicePixelRatio(dpr)
    return pixmap, mon


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
        # Use the *logical*-point monitor size (matching mss.monitors / Qt's own
        # coordinate system), not the screenshot's raw physical-pixel size —
        # see _grab_all_monitors() for why those differ on Retina displays.
        self.setGeometry(
            self._virt_left, self._virt_top,
            int(mon["width"]), int(mon["height"]),
        )

        self._btn = QPushButton("Use Region", self)
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
            dims = f"{self._rect.width()} x {self._rect.height()} pt"
            p.drawText(self._rect.left() + 6, self._rect.top() - 10, dims)

        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        p.drawText(
            self.rect().adjusted(0, 50, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Draw a rectangle over the card screenshot area",
        )
        p.setFont(QFont("Segoe UI", 13))
        p.drawText(
            self.rect().adjusted(0, 95, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Click and drag \u2014 then press Use Region. Press W in the overlay to scan.",
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
