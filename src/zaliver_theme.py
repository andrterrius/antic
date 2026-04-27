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
    selection-background-color: #6366f1;
}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #6366f1;
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

QCheckBox {
    spacing: 10px;
    color: #c7c9d9;
}

QCheckBox::indicator {
    width: 20px;
    height: 20px;
    border-radius: 6px;
    border: 1px solid #3d4258;
    background-color: #0f1118;
}

QCheckBox::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #6366f1, stop:1 #7c3aed);
    border: 1px solid #818cf8;
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
    background-color: rgba(99, 102, 241, 0.16);
    border: 1px solid #6366f1;
}
"""

