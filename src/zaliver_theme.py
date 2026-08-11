"""Zaliver dark theme — QSS + forced dark QPalette for Windows light OS theme."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QApplication

# Shared palette tokens (keep in sync with QSS below)
_BG = "#12141c"
_PANEL = "#1a1d26"
_BASE = "#0f1118"
_BORDER = "#2d3142"
_TEXT = "#e4e6ef"
_MUTED = "#94a3b8"
_HINT = "#6b7280"
_ACCENT = "#6366f1"
_HIGHLIGHT_TEXT = "#f8fafc"


ZALIVER_DARK_QSS = r"""
/* Zaliver dark theme — indigo / violet accents */

* {
    font-family: "Segoe UI", "SF Pro Display", system-ui, sans-serif;
    font-size: 13px;
    color: #e4e6ef;
}

QMainWindow, QWidget#zaliverRoot {
    background-color: #12141c;
}

/* Force dark chrome for surfaces that inherit Windows light palette */
QDialog, QMessageBox, QProgressDialog, QFileDialog {
    background-color: #12141c;
    color: #e4e6ef;
}

QMessageBox QLabel, QDialog QLabel {
    color: #e4e6ef;
    background: transparent;
}

QMenu {
    background-color: #1a1d26;
    color: #e4e6ef;
    border: 1px solid #2d3142;
    padding: 4px;
}

QMenu::item {
    padding: 8px 24px 8px 12px;
    border-radius: 6px;
    background: transparent;
}

QMenu::item:selected {
    background-color: #2e3245;
    color: #f1f5f9;
}

QMenu::separator {
    height: 1px;
    background: #2d3142;
    margin: 4px 8px;
}

QToolTip {
    background-color: #1a1d26;
    color: #e4e6ef;
    border: 1px solid #3d4258;
    padding: 6px 8px;
    border-radius: 6px;
}

QAbstractItemView {
    background-color: #0f1118;
    color: #e4e6ef;
    border: 1px solid #2d3142;
    outline: none;
    selection-background-color: #6366f1;
    selection-color: #f8fafc;
}

QComboBox QAbstractItemView {
    background-color: #0f1118;
    color: #e4e6ef;
    border: 1px solid #2d3142;
    selection-background-color: #6366f1;
    selection-color: #f8fafc;
    outline: none;
    padding: 4px;
}

QHeaderView::section {
    background-color: #252836;
    color: #a5b4fc;
    font-weight: 600;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #3d4258;
}

QGroupBox {
    background-color: #1a1d26;
    border: 1px solid #2d3142;
    border-radius: 12px;
    margin-top: 14px;
    padding: 16px 12px 12px 12px;
    font-weight: 600;
    color: #c7c9d9;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 8px;
    color: #a5b4fc;
}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #0f1118;
    border: 1px solid #2d3142;
    border-radius: 8px;
    padding: 8px 10px;
    color: #e4e6ef;
    selection-background-color: #6366f1;
    selection-color: #f8fafc;
}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #6366f1;
}

QComboBox::drop-down {
    border: none;
    width: 28px;
}

QComboBox::down-arrow {
    width: 10px;
    height: 10px;
}

QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #6366f1, stop:1 #7c3aed);
    color: #f8fafc;
    border: none;
    border-radius: 10px;
    padding: 10px 20px;
    font-weight: 600;
    min-height: 20px;
}

QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #818cf8, stop:1 #8b5cf6);
}

QPushButton:pressed {
    background: #4f46e5;
}

QPushButton#secondary {
    background-color: #252836;
    color: #e4e6ef;
    border: 1px solid #3d4258;
}

QPushButton#secondary:hover {
    background-color: #2e3245;
    border: 1px solid #6366f1;
}

QPushButton#danger {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #be123c, stop:1 #9f1239);
}

QPushButton#danger:hover {
    background: #e11d48;
}

QPlainTextEdit {
    background-color: #0f1118;
    border: 1px solid #2d3142;
    border-radius: 10px;
    padding: 8px;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
    color: #a8b0d4;
}

QProgressBar {
    border: none;
    border-radius: 8px;
    background-color: #1e2230;
    text-align: center;
    color: #e4e6ef;
    min-height: 22px;
    max-height: 22px;
}

QProgressBar::chunk {
    border-radius: 8px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #6366f1, stop:1 #a855f7);
}

QCheckBox, QRadioButton {
    spacing: 10px;
    color: #c7c9d9;
    background: transparent;
}

QCheckBox::indicator, QRadioButton::indicator {
    width: 20px;
    height: 20px;
    border: 1px solid #3d4258;
    background-color: #0f1118;
}

QCheckBox::indicator {
    border-radius: 6px;
}

QRadioButton::indicator {
    border-radius: 10px;
}

QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #6366f1, stop:1 #7c3aed);
    border: 1px solid #818cf8;
}

QLabel {
    color: #e4e6ef;
    background: transparent;
}

QLabel#title {
    font-size: 22px;
    font-weight: 700;
    color: #f1f5f9;
    letter-spacing: 0.5px;
}

QLabel#hint {
    color: #6b7280;
    font-size: 11px;
}

QLabel#profileRowTitle {
    color: #e4e6ef;
    font-weight: 400;
    border: none;
    background: transparent;
}

QLabel#profileRowTitleActive {
    color: #f1f5f9;
    font-weight: 700;
    border: none;
    background: transparent;
}

QLabel#profileRowId {
    color: #94a3b8;
    border: none;
    background: transparent;
}

QLabel#profileRowIdActive {
    color: #cbd5e1;
    border: none;
    background: transparent;
}

/* Строка списка: обычная / открыта в редакторе (клик по строке, не чекбокс) */
QWidget#profileRow {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
}

QWidget#profileRowActive {
    background-color: rgba(139, 92, 246, 0.12);
    border: 2px solid #8b5cf6;
    border-radius: 10px;
}

QLabel#profileRowDesc {
    color: #94a3b8;
    font-size: 12px;
}

QScrollArea {
    background-color: transparent;
}

QScrollArea > QWidget > QWidget {
    background-color: transparent;
}

QSplitter::handle {
    background-color: #2d3142;
    width: 4px;
    border-radius: 2px;
}

QScrollBar:vertical {
    background: #1a1d26;
    width: 10px;
    margin: 0;
    border-radius: 5px;
}

QScrollBar::handle:vertical {
    background: #3d4258;
    min-height: 28px;
    border-radius: 5px;
}

QScrollBar::handle:vertical:hover {
    background: #6366f1;
}

QListWidget#sideNav {
    background-color: #1a1d26;
    border: 1px solid #2d3142;
    border-radius: 12px;
    padding: 8px 4px;
    outline: none;
}

QListWidget#sideNav::item {
    padding: 12px 14px;
    border-radius: 8px;
    color: #c7c9d9;
}

QListWidget#sideNav::item:hover {
    background-color: #252836;
}

QListWidget#sideNav::item:selected {
    background-color: #2e3245;
    border: 1px solid #6366f1;
    color: #f1f5f9;
}

/* Dolphin{anty} profiles list */
QListWidget#profilesList {
    background-color: #0f1118;
    border: 1px solid #2d3142;
    border-radius: 12px;
    padding: 8px;
    outline: none;
}

QListWidget#profilesList::item {
    padding: 4px;
    border-radius: 12px;
    background: transparent;
}

QListWidget#profilesList::item:hover {
    background-color: rgba(99, 102, 241, 0.10);
}

QListWidget#profilesList::item:selected {
    background: transparent;
    border: none;
}

/* Теги профиля (чипы) */
QFrame#tagChip {
    background-color: #252836;
    border: 1px solid #3d4258;
    border-radius: 8px;
}
QFrame#tagChip QLabel {
    color: #e4e6ef;
    font-size: 12px;
    border: none;
    background: transparent;
    padding: 4px 6px;
}
QFrame#error {
    background-color: rgba(190, 18, 60, 0.18);
    border: 1px solid #be123c;
    border-radius: 8px;
}
QFrame#error QLabel {
    color: #fda4af;
    font-size: 12px;
    border: none;
    background: transparent;
    padding: 4px 6px;
}
QFrame#success {
    background-color: rgba(22, 163, 74, 0.18);
    border: 1px solid #16a34a;
    border-radius: 8px;
}
QFrame#success QLabel {
    color: #86efac;
    font-size: 12px;
    border: none;
    background: transparent;
    padding: 4px 6px;
}
QPushButton#tagChipClose {
    background: transparent;
    border: none;
    color: #94a3b8;
    font-weight: bold;
    font-size: 14px;
    min-width: 20px;
    max-width: 22px;
    min-height: 20px;
    max-height: 22px;
    padding: 0px;
    border-radius: 4px;
}
QPushButton#tagChipClose:hover {
    color: #f1f5f9;
    background-color: rgba(239, 68, 68, 0.22);
}

/* Вкладка «Прокси» */
QFrame#proxiesHeader {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(99, 102, 241, 0.14), stop:1 rgba(124, 58, 237, 0.10));
    border: 1px solid #3d4258;
    border-radius: 14px;
    padding: 4px;
}

QLabel#proxiesHeaderHint {
    color: #94a3b8;
    font-size: 12px;
    line-height: 1.4;
}

QFrame#proxiesStatCard {
    background-color: #1a1d26;
    border: 1px solid #2d3142;
    border-radius: 12px;
}

QFrame#proxiesStatCardOk {
    background-color: rgba(22, 163, 74, 0.10);
    border: 1px solid rgba(22, 163, 74, 0.45);
    border-radius: 12px;
}

QFrame#proxiesStatCardFail {
    background-color: rgba(190, 18, 60, 0.10);
    border: 1px solid rgba(190, 18, 60, 0.45);
    border-radius: 12px;
}

QFrame#proxiesStatCardUnknown {
    background-color: rgba(100, 116, 139, 0.12);
    border: 1px solid #3d4258;
    border-radius: 12px;
}

QLabel#proxiesStatValue {
    font-size: 26px;
    font-weight: 700;
    color: #f1f5f9;
}

QLabel#proxiesStatValueOk {
    font-size: 26px;
    font-weight: 700;
    color: #86efac;
}

QLabel#proxiesStatValueFail {
    font-size: 26px;
    font-weight: 700;
    color: #fda4af;
}

QLabel#proxiesStatValueUnknown {
    font-size: 26px;
    font-weight: 700;
    color: #cbd5e1;
}

QLabel#proxiesStatLabel {
    color: #94a3b8;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
}

QFrame#proxiesTableFrame {
    background-color: #1a1d26;
    border: 1px solid #2d3142;
    border-radius: 14px;
}

QTableWidget#proxiesTable {
    background-color: #0f1118;
    border: none;
    border-radius: 10px;
    gridline-color: #252836;
    outline: none;
    selection-background-color: rgba(99, 102, 241, 0.28);
    selection-color: #f1f5f9;
    alternate-background-color: rgba(255, 255, 255, 0.02);
    color: #e4e6ef;
}

QTableWidget#proxiesTable::item {
    padding: 8px 10px;
    border: none;
    color: #e4e6ef;
}

QTableWidget#proxiesTable QHeaderView::section {
    background-color: #252836;
    color: #a5b4fc;
    font-weight: 600;
    padding: 10px 12px;
    border: none;
    border-bottom: 1px solid #3d4258;
}

QPushButton#proxiesTableRefreshBtn {
    background-color: #252836;
    border: 1px solid #3d4258;
    border-radius: 8px;
    padding: 4px;
    min-height: 0;
}

QPushButton#proxiesTableRefreshBtn:hover {
    background-color: #2e3245;
    border: 1px solid #6366f1;
}

QFrame#proxyProfileIdChip {
    background-color: #252836;
    border: 1px solid #3d4258;
    border-radius: 8px;
}

QFrame#proxyProfileIdChip:hover {
    border: 1px solid #6366f1;
}

QLabel#proxyProfileIdLabel {
    color: #c7d2fe;
    border: none;
    background: transparent;
    padding: 0;
}

QPushButton#proxyProfileIdOpenBtn {
    background: transparent;
    border: none;
    color: #c7d2fe;
    text-align: left;
    padding: 0 2px;
    min-height: 0;
    font-weight: 500;
}

QPushButton#proxyProfileIdOpenBtn:hover {
    color: #f1f5f9;
    text-decoration: underline;
}

QPushButton#proxyProfileIdOpenBtn:pressed {
    color: #a5b4fc;
}

QPushButton#proxyIdCopyBtn,
QPushButton#profileIdCopyBtn {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px;
    min-height: 0;
}

QPushButton#proxyIdCopyBtn:hover,
QPushButton#profileIdCopyBtn:hover {
    background-color: rgba(99, 102, 241, 0.22);
    border: 1px solid #6366f1;
}

QScrollArea#proxyProfileIdsScroll {
    background: transparent;
}

/* Вкладка «2FA» */
QFrame#twofaHeader {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(99, 102, 241, 0.14), stop:1 rgba(124, 58, 237, 0.10));
    border: 1px solid #3d4258;
    border-radius: 14px;
    padding: 4px;
}

QLabel#twofaHeaderHint {
    color: #94a3b8;
    font-size: 12px;
    line-height: 1.4;
}

QFrame#twofaQuickPanel {
    background-color: #1a1d26;
    border: 1px solid #2d3142;
    border-radius: 14px;
}

QLabel#twofaQuickTitle {
    color: #a5b4fc;
    font-size: 14px;
    font-weight: 600;
}

QLabel#twofaQuickHint {
    color: #94a3b8;
    font-size: 12px;
}

QFrame#twofaStatCard {
    background-color: #1a1d26;
    border: 1px solid #2d3142;
    border-radius: 12px;
}

QFrame#twofaStatCardOk {
    background-color: rgba(22, 163, 74, 0.10);
    border: 1px solid rgba(22, 163, 74, 0.45);
    border-radius: 12px;
}

QFrame#twofaStatCardUnknown {
    background-color: rgba(100, 116, 139, 0.12);
    border: 1px solid #3d4258;
    border-radius: 12px;
}

QLabel#twofaStatValue {
    font-size: 26px;
    font-weight: 700;
    color: #f1f5f9;
}

QLabel#twofaStatValueOk {
    font-size: 26px;
    font-weight: 700;
    color: #86efac;
}

QLabel#twofaStatValueUnknown {
    font-size: 26px;
    font-weight: 700;
    color: #cbd5e1;
}

QLabel#twofaStatLabel {
    color: #94a3b8;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
}

QFrame#twofaTableFrame {
    background-color: #1a1d26;
    border: 1px solid #2d3142;
    border-radius: 14px;
}

QTableWidget#twofaTable {
    background-color: #0f1118;
    border: none;
    border-radius: 10px;
    gridline-color: #252836;
    outline: none;
    selection-background-color: rgba(99, 102, 241, 0.28);
    selection-color: #f1f5f9;
    alternate-background-color: rgba(255, 255, 255, 0.02);
    color: #e4e6ef;
}

QTableWidget#twofaTable::item {
    padding: 8px 10px;
    border: none;
    color: #e4e6ef;
}

QTableWidget#twofaTable QHeaderView::section {
    background-color: #252836;
    color: #a5b4fc;
    font-weight: 600;
    padding: 10px 12px;
    border: none;
    border-bottom: 1px solid #3d4258;
}

QLabel#twofaCodeLabel {
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 18px;
    font-weight: 700;
    color: #f1f5f9;
    letter-spacing: 2px;
    padding: 2px 4px;
    border-radius: 6px;
}

QLabel#twofaCodeLabel:hover {
    color: #c7d2fe;
    background-color: rgba(99, 102, 241, 0.12);
}

QLabel#twofaTimerLabel {
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 14px;
    font-weight: 600;
    color: #a5b4fc;
}

QPushButton#twofaCopyBtn {
    background-color: #252836;
    border: 1px solid #3d4258;
    border-radius: 8px;
    padding: 4px 8px;
    min-height: 0;
    font-size: 12px;
}

QPushButton#twofaCopyBtn:hover {
    background-color: #2e3245;
    border: 1px solid #6366f1;
}
"""


def apply_zaliver_dark_theme(app: "QApplication") -> None:
    """Force dark UI regardless of Windows light / high-contrast OS theme."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPalette

    # Fusion paints consistently with QPalette; native Windows style ignores parts of QSS.
    app.setStyle("Fusion")

    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except Exception:
        pass

    bg = QColor(_BG)
    panel = QColor(_PANEL)
    base = QColor(_BASE)
    text = QColor(_TEXT)
    muted = QColor(_MUTED)
    hint = QColor(_HINT)
    accent = QColor(_ACCENT)
    highlight_text = QColor(_HIGHLIGHT_TEXT)
    border = QColor(_BORDER)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, bg)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, panel)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.BrightText, highlight_text)
    palette.setColor(QPalette.ColorRole.Button, panel)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.Light, border)
    palette.setColor(QPalette.ColorRole.Midlight, QColor("#252836"))
    palette.setColor(QPalette.ColorRole.Mid, border)
    palette.setColor(QPalette.ColorRole.Dark, base)
    palette.setColor(QPalette.ColorRole.Shadow, QColor("#000000"))
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, highlight_text)
    palette.setColor(QPalette.ColorRole.Link, accent)
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor("#a855f7"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.PlaceholderText, muted)

    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.ToolTipText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, hint)

    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, panel)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, panel)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Window, bg)

    app.setPalette(palette)
    app.setStyleSheet(ZALIVER_DARK_QSS)
