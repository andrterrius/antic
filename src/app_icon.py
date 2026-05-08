"""Window / taskbar icon: indigo→violet gradient and «A», matching Zaliver theme buttons."""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap


# Same stops as QPushButton in zaliver_theme.py
_GRAD_START = QColor(0x63, 0x66, 0xF1)
_GRAD_END = QColor(0x7C, 0x3A, 0xED)
_LETTER = QColor(0xF8, 0xFA, 0xFC)


def _render_icon_pixmap(edge: int) -> QPixmap:
    pm = QPixmap(edge, edge)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    r = float(edge)
    radius = r * 0.22
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, r, r), radius, radius)

    g = QLinearGradient(0, 0, r, r)
    g.setColorAt(0, _GRAD_START)
    g.setColorAt(1, _GRAD_END)
    p.fillPath(path, QBrush(g))

    p.setPen(QPen(QColor(0x81, 0x8C, 0xF8, 90), max(1, edge // 128)))
    p.drawPath(path)

    f = QFont("Segoe UI")
    f.setPixelSize(max(10, int(edge * 0.52)))
    f.setWeight(QFont.Weight.Bold)
    p.setFont(f)
    p.setPen(_LETTER)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "A")
    p.end()
    return pm


def build_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(_render_icon_pixmap(size))
    return icon
