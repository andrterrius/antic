from __future__ import annotations

import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QFileDialog,
    QDoubleSpinBox,
)

from profiles_store import BrowserProfile, load_profiles, save_profiles
from fingerprint_generator import generate_test_fingerprint
from playwright_runner import run_profile, profile_user_data_dir
from zaliver_theme import ZALIVER_DARK_QSS


class RunnerThread(QThread):
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, profile: BrowserProfile, start_url: str, script_path: Optional[str]) -> None:
        super().__init__()
        self._profile = profile
        self._start_url = start_url
        self._script_path = script_path

    def run(self) -> None:
        res = run_profile(
            self._profile,
            start_url=self._start_url,
            script_path=self._script_path,
            log=lambda s: self.log_line.emit(s),
        )
        self.finished_ok.emit(res.ok, res.message)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Antidetect (Playwright Profiles) — UI")
        self.setMinimumSize(1060, 680)

        root = QWidget()
        root.setObjectName("zaliverRoot")
        self.setCentralWidget(root)

        self._profiles: list[BrowserProfile] = load_profiles()
        self._active_profile_id: Optional[str] = self._profiles[0].profile_id if self._profiles else None
        self._runners: dict[str, RunnerThread] = {}

        layout = QHBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # left nav
        nav = QListWidget()
        nav.setObjectName("sideNav")
        nav.addItem("Profiles")
        nav.addItem("Automation")
        nav.setFixedWidth(180)
        nav.setCurrentRow(0)

        # pages
        self.pages = QStackedWidget()
        self.page_profiles = self._build_profiles_page()
        self.page_automation = self._build_automation_page()
        self.pages.addWidget(self.page_profiles)
        self.pages.addWidget(self.page_automation)

        splitter.addWidget(nav)
        splitter.addWidget(self.pages)
        splitter.setStretchFactor(1, 1)

        nav.currentRowChanged.connect(self.pages.setCurrentIndex)

        self._apply_theme()
        self._refresh_profiles_list()
        self._load_active_profile_into_form()

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app:
            app.setStyleSheet(ZALIVER_DARK_QSS)

    def _build_profiles_page(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(14)

        title = QLabel("Profiles")
        title.setObjectName("title")
        hint = QLabel("Профили Playwright: прокси + параметры контекста для тестирования.")
        hint.setObjectName("hint")
        l.addWidget(title)
        l.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(14)
        l.addLayout(body, 1)

        # list box
        list_box = QGroupBox("Profiles list")
        list_layout = QVBoxLayout(list_box)
        list_layout.setSpacing(10)

        self.profiles_list = QListWidget()
        self.profiles_list.setObjectName("profilesList")
        self.profiles_list.currentRowChanged.connect(self._on_profile_selected)
        list_layout.addWidget(self.profiles_list, 1)

        btn_row = QHBoxLayout()
        self.btn_new = QPushButton("New")
        self.btn_new.setObjectName("secondary")
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setObjectName("danger")
        btn_row.addWidget(self.btn_new)
        btn_row.addWidget(self.btn_delete)
        list_layout.addLayout(btn_row)

        self.btn_new.clicked.connect(self._create_profile)
        self.btn_delete.clicked.connect(self._delete_profile)

        body.addWidget(list_box, 1)

        # editor box
        editor = QGroupBox("Profile settings")
        form = QFormLayout(editor)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.ed_name = QLineEdit()
        self.ed_proxy_server = QLineEdit()
        self.ed_proxy_server.setPlaceholderText("http://host:port or socks5://host:port")
        self.ed_proxy_user = QLineEdit()
        self.ed_proxy_pass = QLineEdit()
        self.ed_proxy_pass.setEchoMode(QLineEdit.EchoMode.Password)

        self.ed_ua = QLineEdit()
        self.ed_locale = QLineEdit()
        self.ed_locale.setPlaceholderText("en-US")
        self.ed_tz = QLineEdit()
        self.ed_tz.setPlaceholderText("Europe/Moscow")

        self.cb_engine = QComboBox()
        self.cb_engine.addItem("Chromium", userData="chromium")
        self.cb_engine.addItem("Firefox", userData="firefox")
        self.cb_engine.addItem("WebKit", userData="webkit")

        self.cb_device = QComboBox()
        self.cb_device.addItem("None (Desktop)", userData=None)
        self.cb_device.addItem("iPhone 13", userData="iPhone 13")
        self.cb_device.addItem("Pixel 7", userData="Pixel 7")

        self.cb_color = QComboBox()
        self.cb_color.addItem("No preference", userData=None)
        self.cb_color.addItem("Light", userData="light")
        self.cb_color.addItem("Dark", userData="dark")

        self.sp_w = QSpinBox()
        self.sp_w.setRange(320, 3840)
        self.sp_h = QSpinBox()
        self.sp_h.setRange(240, 2160)

        self.sp_lat = QDoubleSpinBox()
        self.sp_lat.setRange(-90.0, 90.0)
        self.sp_lat.setDecimals(6)
        self.sp_lat.setSingleStep(0.1)
        self.sp_lon = QDoubleSpinBox()
        self.sp_lon.setRange(-180.0, 180.0)
        self.sp_lon.setDecimals(6)
        self.sp_lon.setSingleStep(0.1)

        form.addRow("Name", self.ed_name)
        form.addRow("Proxy server", self.ed_proxy_server)
        form.addRow("Proxy username", self.ed_proxy_user)
        form.addRow("Proxy password", self.ed_proxy_pass)
        form.addRow(self._hr())
        form.addRow("Engine", self.cb_engine)
        form.addRow("Device preset", self.cb_device)
        form.addRow("User-Agent", self.ed_ua)
        form.addRow("Locale", self.ed_locale)
        form.addRow("Timezone", self.ed_tz)
        form.addRow("Color scheme", self.cb_color)
        form.addRow("Viewport width", self.sp_w)
        form.addRow("Viewport height", self.sp_h)
        form.addRow("Geo latitude", self.sp_lat)
        form.addRow("Geo longitude", self.sp_lon)

        actions = QHBoxLayout()
        self.btn_save = QPushButton("Save profile")
        self.btn_save.setObjectName("secondary")
        actions.addStretch(1)
        actions.addWidget(self.btn_save)
        self.btn_save.clicked.connect(self._save_active_profile)

        l2 = QVBoxLayout()
        l2.addWidget(editor, 1)
        l2.addLayout(actions)
        body.addLayout(l2, 2)

        return w

    def _build_automation_page(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(14)

        title = QLabel("Automation")
        title.setObjectName("title")
        hint = QLabel("Запуск профиля (persistent context) + опциональный python‑скрипт с run(page, log).")
        hint.setObjectName("hint")
        l.addWidget(title)
        l.addWidget(hint)

        box = QGroupBox("Launch")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.launch_profiles = QListWidget()
        self.launch_profiles.setObjectName("profilesList")
        self.launch_profiles.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.launch_profiles.setMinimumHeight(160)
        self.ed_url = QLineEdit("https://example.com")
        self.ed_script = QLineEdit()
        self.ed_script.setPlaceholderText("Optional automation script (.py)")
        btn_pick = QPushButton("Browse…")
        btn_pick.setObjectName("secondary")
        btn_pick.clicked.connect(self._pick_script)

        script_row = QHBoxLayout()
        script_row.addWidget(self.ed_script, 1)
        script_row.addWidget(btn_pick)

        form.addRow("Profiles (multi)", self.launch_profiles)
        form.addRow("Start URL", self.ed_url)
        form.addRow("Script", script_row)

        btns = QHBoxLayout()
        self.btn_launch = QPushButton("Launch selected")
        self.btn_launch.setObjectName("secondary")
        self.btn_launch.clicked.connect(self._launch_selected)
        self.btn_launch_all = QPushButton("Launch all")
        self.btn_launch_all.setObjectName("secondary")
        self.btn_launch_all.clicked.connect(self._launch_all)
        btns.addStretch(1)
        btns.addWidget(self.btn_launch)
        btns.addWidget(self.btn_launch_all)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)

        l.addWidget(box)
        l.addLayout(btns)
        l.addWidget(QGroupBox("Logs"), 0)
        l.addWidget(self.log, 1)

        return w

    def _hr(self) -> QWidget:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _refresh_profiles_list(self) -> None:
        self.profiles_list.blockSignals(True)
        self.profiles_list.clear()
        for p in self._profiles:
            item = QListWidgetItem(f"{p.name}\n{p.profile_id}")
            item.setData(Qt.ItemDataRole.UserRole, p.profile_id)
            self.profiles_list.addItem(item)
        self.profiles_list.blockSignals(False)

        # automation multi list (checkable)
        self.launch_profiles.blockSignals(True)
        self.launch_profiles.clear()
        for p in self._profiles:
            it = QListWidgetItem(f"{p.name}  ({p.profile_id})")
            it.setData(Qt.ItemDataRole.UserRole, p.profile_id)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.launch_profiles.addItem(it)
        self.launch_profiles.blockSignals(False)

        if self._active_profile_id:
            idx = self._index_by_id(self._active_profile_id)
            if idx is not None:
                self.profiles_list.setCurrentRow(idx)

    def _index_by_id(self, profile_id: str) -> Optional[int]:
        for i, p in enumerate(self._profiles):
            if p.profile_id == profile_id:
                return i
        return None

    def _active_profile(self) -> Optional[BrowserProfile]:
        if not self._active_profile_id:
            return None
        for p in self._profiles:
            if p.profile_id == self._active_profile_id:
                return p
        return None

    def _on_profile_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._profiles):
            self._active_profile_id = None
            return
        self._active_profile_id = self._profiles[row].profile_id
        self._load_active_profile_into_form()

    def _load_active_profile_into_form(self) -> None:
        p = self._active_profile()
        if not p:
            return
        self.ed_name.setText(p.name)
        self.ed_proxy_server.setText(p.proxy_server or "")
        self.ed_proxy_user.setText(p.proxy_username or "")
        self.ed_proxy_pass.setText(p.proxy_password or "")
        self._set_combo_by_data(self.cb_engine, p.engine or "chromium")
        self._set_combo_by_data(self.cb_device, p.device_preset)
        self.ed_ua.setText(p.user_agent or "")
        self.ed_locale.setText(p.locale or "")
        self.ed_tz.setText(p.timezone_id or "")
        self._set_combo_by_data(self.cb_color, p.color_scheme)
        self.sp_w.setValue(int(p.viewport_width or 1280))
        self.sp_h.setValue(int(p.viewport_height or 720))
        self.sp_lat.setValue(float(p.geo_lat or 0.0))
        self.sp_lon.setValue(float(p.geo_lon or 0.0))

    def _create_profile(self) -> None:
        new_id = uuid.uuid4().hex[:12]
        base = BrowserProfile(profile_id=new_id, name=f"Profile {len(self._profiles) + 1}")
        p = generate_test_fingerprint(base)
        self._profiles.append(p)
        self._active_profile_id = p.profile_id
        save_profiles(self._profiles)
        self._refresh_profiles_list()
        self._load_active_profile_into_form()

    def _delete_profile(self) -> None:
        p = self._active_profile()
        if not p:
            return
        res = QMessageBox.question(self, "Delete profile", f"Delete '{p.name}'?\n\nID: {p.profile_id}")
        if res != QMessageBox.StandardButton.Yes:
            return

        # Stop running instance if any (user should close browser window, but we can prevent deletion while running)
        r = self._runners.get(p.profile_id)
        if r and r.isRunning():
            QMessageBox.warning(self, "Running", "This profile is currently running. Close the browser window first.")
            return

        # Remove on-disk storage for the profile (Playwright persistent context directory)
        try:
            shutil.rmtree(profile_user_data_dir(p.profile_id), ignore_errors=True)
        except Exception:
            # keep going; profile removal should not be blocked by fs issues
            pass

        self._profiles = [x for x in self._profiles if x.profile_id != p.profile_id]
        self._active_profile_id = self._profiles[0].profile_id if self._profiles else None
        save_profiles(self._profiles)
        self._refresh_profiles_list()
        self._load_active_profile_into_form()

    def _save_active_profile(self) -> None:
        p = self._active_profile()
        if not p:
            return
        updated = replace(
            p,
            name=self.ed_name.text().strip() or p.name,
            proxy_server=self._blank_to_none(self.ed_proxy_server.text()),
            proxy_username=self._blank_to_none(self.ed_proxy_user.text()),
            proxy_password=self._blank_to_none(self.ed_proxy_pass.text()),
            engine=str(self.cb_engine.currentData() or "chromium"),
            device_preset=self.cb_device.currentData(),
            user_agent=self._blank_to_none(self.ed_ua.text()),
            locale=self._blank_to_none(self.ed_locale.text()),
            timezone_id=self._blank_to_none(self.ed_tz.text()),
            color_scheme=self.cb_color.currentData(),
            viewport_width=int(self.sp_w.value()),
            viewport_height=int(self.sp_h.value()),
            geo_lat=self._geo_or_none(self.sp_lat.value()),
            geo_lon=self._geo_or_none(self.sp_lon.value()),
        )
        self._profiles = [updated if x.profile_id == updated.profile_id else x for x in self._profiles]
        save_profiles(self._profiles)
        self._refresh_profiles_list()

    def _blank_to_none(self, s: str) -> Optional[str]:
        s2 = (s or "").strip()
        return s2 if s2 else None

    def _set_combo_by_data(self, cb: QComboBox, data) -> None:
        for i in range(cb.count()):
            if cb.itemData(i) == data:
                cb.setCurrentIndex(i)
                return
        # fallback to first
        if cb.count():
            cb.setCurrentIndex(0)

    def _geo_or_none(self, v: float) -> float | None:
        # Treat 0.0/0.0 as "not set" to avoid accidental geolocation permission.
        if abs(v) < 1e-12:
            return None
        return float(v)

    def _pick_script(self) -> None:
        start_dir = str(Path(__file__).resolve().parent)
        p, _ = QFileDialog.getOpenFileName(self, "Select script", start_dir, "Python (*.py)")
        if p:
            self.ed_script.setText(p)

    def _checked_profile_ids(self) -> list[str]:
        ids: list[str] = []
        for i in range(self.launch_profiles.count()):
            it = self.launch_profiles.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                pid = it.data(Qt.ItemDataRole.UserRole)
                if pid:
                    ids.append(str(pid))
        return ids

    def _launch_selected(self) -> None:
        ids = self._checked_profile_ids()
        if not ids:
            QMessageBox.information(self, "Select profiles", "Check one or more profiles to launch.")
            return
        self._launch_profiles(ids)

    def _launch_all(self) -> None:
        ids = [p.profile_id for p in self._profiles]
        if not ids:
            QMessageBox.information(self, "No profiles", "Create a profile first.")
            return
        self._launch_profiles(ids)

    def _launch_profiles(self, ids: list[str]) -> None:
        url = self.ed_url.text().strip() or "https://example.com"
        script = self.ed_script.text().strip() or None

        for profile_id in ids:
            if profile_id in self._runners and self._runners[profile_id].isRunning():
                self._append_log(f"[{profile_id}] already running — skip")
                continue

            p = next((x for x in self._profiles if x.profile_id == profile_id), None)
            if not p:
                self._append_log(f"[{profile_id}] not found — skip")
                continue

            prefix = f"[{p.name}:{p.profile_id}]"
            self._append_log(f"{prefix} launch")
            runner = RunnerThread(p, url, script)
            runner.log_line.connect(lambda s, pref=prefix: self._append_log(f"{pref} {s}"))
            runner.finished_ok.connect(lambda ok, msg, pref=prefix, pid=profile_id: self._on_runner_finished(pid, pref, ok, msg))
            self._runners[profile_id] = runner
            runner.start()

    def _on_runner_finished(self, profile_id: str, prefix: str, ok: bool, msg: str) -> None:
        self._append_log(f"{prefix} finished: {'OK' if ok else 'FAIL'} — {msg}")
        r = self._runners.get(profile_id)
        if r and not r.isRunning():
            self._runners.pop(profile_id, None)

    def _append_log(self, s: str) -> None:
        self.log.appendPlainText(s)


def run_qt() -> None:
    app = QApplication([])
    app.setApplicationName("Antidetect UI")
    app.setStyleSheet(ZALIVER_DARK_QSS)
    w = MainWindow()
    w.show()
    app.exec()

