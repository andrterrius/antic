from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import replace
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
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QDoubleSpinBox,
    QSizePolicy,
)

from profiles_store import BrowserProfile, load_profiles, save_profiles
from fingerprint_generator import generate_test_fingerprint
from playwright_runner import run_profile, profile_user_data_dir, get_proxy_ip, geoip_from_ip
from fingerprint_consistency import normalize_timezone_country
from zaliver_theme import ZALIVER_DARK_QSS


class RunnerThread(QThread):
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, profile: BrowserProfile, start_url: str, script_path: Optional[str]) -> None:
        super().__init__()
        self._profile = profile
        self._start_url = start_url
        self._script_path = script_path
        self._stop_evt = threading.Event()

    def request_stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        res = run_profile(
            self._profile,
            start_url=self._start_url,
            script_path=self._script_path,
            log=lambda s: self.log_line.emit(s),
            stop_requested=self._stop_evt.is_set,
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
        self._run_buttons: dict[str, QPushButton] = {}
        self._profile_row_widget_to_id: dict[int, str] = {}
        self._profile_id_to_item: dict[str, QListWidgetItem] = {}

        layout = QHBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # left nav
        nav = QListWidget()
        nav.setObjectName("sideNav")
        nav.addItem("Профили")
        nav.setFixedWidth(180)
        nav.setCurrentRow(0)

        # pages
        self.pages = QStackedWidget()
        self.page_profiles = self._build_profiles_page()
        self.pages.addWidget(self.page_profiles)

        splitter.addWidget(nav)
        splitter.addWidget(self.pages)
        splitter.setStretchFactor(1, 1)

        nav.currentRowChanged.connect(self.pages.setCurrentIndex)

        self._apply_theme()
        self._refresh_profiles_list()
        self._load_active_profile_into_form()

    def _expand_field(self, w: QWidget, *, min_w: int = 420) -> None:
        sp = w.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
        w.setSizePolicy(sp)
        w.setMinimumWidth(min_w)

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app:
            app.setStyleSheet(ZALIVER_DARK_QSS)

    def _build_profiles_page(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(14)

        title = QLabel("Профили")
        title.setObjectName("title")
        l.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(14)
        l.addLayout(body, 1)

        # list box
        list_box = QGroupBox("Список профилей")
        list_layout = QVBoxLayout(list_box)
        list_layout.setSpacing(10)

        self.profiles_list = QListWidget()
        self.profiles_list.setObjectName("profilesList")
        # Multi-select for batch launching; editing still follows "current" item.
        self.profiles_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.profiles_list.currentItemChanged.connect(self._on_profile_current_changed)
        self.profiles_list.itemClicked.connect(self._on_profile_item_clicked)
        list_layout.addWidget(self.profiles_list, 1)

        launch_box = QGroupBox("Запуск")
        launch_form = QFormLayout(launch_box)
        launch_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        launch_form.setHorizontalSpacing(12)
        launch_form.setVerticalSpacing(10)
        launch_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.ed_url = QLineEdit("https://2ip.ru")
        self._expand_field(self.ed_url)
        launch_form.addRow("Стартовый URL", self.ed_url)

        launch_btns = QHBoxLayout()
        self.btn_launch_selected = QPushButton("Запустить выбранные")
        self.btn_launch_selected.setObjectName("secondary")
        self.btn_launch_selected.clicked.connect(self._launch_selected_from_profiles_list)
        self.btn_launch_all = QPushButton("Запустить все")
        self.btn_launch_all.setObjectName("secondary")
        self.btn_launch_all.clicked.connect(self._launch_all)
        launch_btns.addStretch(1)
        launch_btns.addWidget(self.btn_launch_selected)
        launch_btns.addWidget(self.btn_launch_all)
        launch_form.addRow("", launch_btns)

        list_layout.addWidget(launch_box)

        btn_row = QHBoxLayout()
        self.btn_new = QPushButton("Новый")
        self.btn_new.setObjectName("secondary")
        self.btn_delete = QPushButton("Удалить")
        self.btn_delete.setObjectName("danger")
        btn_row.addWidget(self.btn_new)
        btn_row.addWidget(self.btn_delete)
        list_layout.addLayout(btn_row)

        self.btn_new.clicked.connect(self._create_profile)
        self.btn_delete.clicked.connect(self._delete_profile)

        body.addWidget(list_box, 1)

        # editor box
        editor = QGroupBox("Настройки профиля")
        form = QFormLayout(editor)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

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
        self.ed_country = QLineEdit()
        self.ed_country.setPlaceholderText("RU")
        self.ed_webgl_vendor = QLineEdit()
        self.ed_webgl_vendor.setPlaceholderText("Google Inc.")
        self.ed_webgl_renderer = QLineEdit()
        self.ed_webgl_renderer.setPlaceholderText("ANGLE (...)")
        self.ed_webgl_version = QLineEdit()
        self.ed_webgl_version.setPlaceholderText("WebGL 1.0 (OpenGL ES 2.0 Chromium) — по умолчанию")
        self.ed_webgl_slv = QLineEdit()
        self.ed_webgl_slv.setPlaceholderText("WebGL GLSL ES 1.0 … — по умолчанию")

        for _w in (
            self.ed_name,
            self.ed_proxy_server,
            self.ed_proxy_user,
            self.ed_proxy_pass,
            self.ed_ua,
            self.ed_locale,
            self.ed_tz,
            self.ed_country,
            self.ed_webgl_vendor,
            self.ed_webgl_renderer,
            self.ed_webgl_version,
            self.ed_webgl_slv,
        ):
            self._expand_field(_w)

        # If proxy changes, regenerate persona (but don't auto-save).
        self.ed_proxy_server.editingFinished.connect(self._on_proxy_fields_edited)
        self.ed_proxy_user.editingFinished.connect(self._on_proxy_fields_edited)
        self.ed_proxy_pass.editingFinished.connect(self._on_proxy_fields_edited)
        # Live-toggle locale/tz behavior when proxy becomes empty/non-empty.
        self.ed_proxy_server.textChanged.connect(lambda _t: self._sync_locale_tz_system_mode())

        self.cb_color = QComboBox()
        self.cb_color.addItem("Не важно", userData=None)
        self.cb_color.addItem("Светлая", userData="light")
        self.cb_color.addItem("Тёмная", userData="dark")
        self._expand_field(self.cb_color, min_w=260)

        self.sp_lat = QDoubleSpinBox()
        self.sp_lat.setRange(-90.0, 90.0)
        self.sp_lat.setDecimals(6)
        self.sp_lat.setSingleStep(0.1)
        self.sp_lon = QDoubleSpinBox()
        self.sp_lon.setRange(-180.0, 180.0)
        self.sp_lon.setDecimals(6)
        self.sp_lon.setSingleStep(0.1)
        self._expand_field(self.sp_lat, min_w=220)
        self._expand_field(self.sp_lon, min_w=220)

        form.addRow("Имя", self.ed_name)
        form.addRow("Прокси (сервер)", self.ed_proxy_server)
        form.addRow("Прокси (логин)", self.ed_proxy_user)
        form.addRow("Прокси (пароль)", self.ed_proxy_pass)
        form.addRow(self._hr())
        form.addRow("User-Agent", self.ed_ua)
        form.addRow("Locale", self.ed_locale)
        form.addRow("Часовой пояс", self.ed_tz)
        form.addRow("Страна (ISO2)", self.ed_country)
        form.addRow("Цветовая схема", self.cb_color)
        form.addRow("Гео (широта)", self.sp_lat)
        form.addRow("Гео (долгота)", self.sp_lon)
        form.addRow(self._hr())
        form.addRow("WebGL vendor", self.ed_webgl_vendor)
        form.addRow("WebGL renderer", self.ed_webgl_renderer)
        form.addRow("WebGL VERSION (GL1)", self.ed_webgl_version)
        form.addRow("WebGL SHADING_LANGUAGE_VERSION (GL1)", self.ed_webgl_slv)

        actions = QHBoxLayout()
        self.btn_save = QPushButton("Сохранить профиль")
        self.btn_save.setObjectName("secondary")
        actions.addStretch(1)
        actions.addWidget(self.btn_save)
        self.btn_save.clicked.connect(self._save_active_profile)

        l2 = QVBoxLayout()
        l2.addWidget(editor, 1)
        l2.addLayout(actions)
        body.addLayout(l2, 2)

        logs_box = QGroupBox("Логи")
        logs_l = QVBoxLayout(logs_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        logs_l.addWidget(self.log, 1)
        l.addWidget(logs_box, 1)

        return w

    def _hr(self) -> QWidget:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _refresh_profiles_list(self) -> None:
        self.profiles_list.blockSignals(True)
        self.profiles_list.clear()
        self._run_buttons.clear()
        self._profile_row_widget_to_id.clear()
        self._profile_id_to_item.clear()
        for p in self._profiles:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, p.profile_id)
            it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self.profiles_list.addItem(it)
            self._profile_id_to_item[p.profile_id] = it

            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(10, 6, 10, 6)
            row_l.setSpacing(10)

            lbl = QLabel(f"{p.name}  ({p.profile_id})")
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            # Double-click on the row/label should open settings for that profile.
            row.installEventFilter(self)
            lbl.installEventFilter(self)
            self._profile_row_widget_to_id[id(row)] = p.profile_id
            self._profile_row_widget_to_id[id(lbl)] = p.profile_id

            btn_run = QPushButton()
            self._run_buttons[p.profile_id] = btn_run
            btn_run.clicked.connect(lambda _checked=False, pid=p.profile_id: self._run_button_clicked(pid))
            self._sync_run_button(p.profile_id)

            row_l.addWidget(lbl, 1)
            row_l.addWidget(btn_run, 0, Qt.AlignmentFlag.AlignRight)

            it.setSizeHint(row.sizeHint())
            self.profiles_list.setItemWidget(it, row)
        self.profiles_list.blockSignals(False)

        if self._active_profile_id:
            idx = self._index_by_id(self._active_profile_id)
            if idx is not None:
                self.profiles_list.setCurrentRow(idx)

    def _is_profile_running(self, profile_id: str) -> bool:
        r = self._runners.get(profile_id)
        return bool(r and r.isRunning())

    def _sync_run_button(self, profile_id: str) -> None:
        btn = self._run_buttons.get(profile_id)
        if not btn:
            return
        running = self._is_profile_running(profile_id)
        btn.setText("Остановить" if running else "Запустить")
        btn.setObjectName("danger" if running else "secondary")
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _run_button_clicked(self, profile_id: str) -> None:
        if self._is_profile_running(profile_id):
            self._stop_profile(profile_id)
            return
        self._launch_profiles([profile_id])

    def _stop_profile(self, profile_id: str) -> None:
        r = self._runners.get(profile_id)
        if not r or not r.isRunning():
            self._sync_run_button(profile_id)
            return
        self._append_log(f"[{profile_id}] stop requested")
        r.request_stop()
        # keep button in "stop" state until runner finishes
        self._sync_run_button(profile_id)

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

    def _on_profile_current_changed(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if not current:
            self._active_profile_id = None
            return
        pid = current.data(Qt.ItemDataRole.UserRole)
        if not pid:
            self._active_profile_id = None
            return
        self._active_profile_id = str(pid)
        self._load_active_profile_into_form()

    def _on_profile_item_clicked(self, item: QListWidgetItem) -> None:
        pid = item.data(Qt.ItemDataRole.UserRole)
        if not pid:
            return
        self._select_profile(str(pid))

    def _select_profile(self, profile_id: str) -> None:
        it = self._profile_id_to_item.get(profile_id)
        if not it:
            return
        self.profiles_list.setCurrentItem(it)
        self.profiles_list.scrollToItem(it)
        self._active_profile_id = profile_id
        self._load_active_profile_into_form()

    def eventFilter(self, watched: object, event: object) -> bool:  # type: ignore[override]
        # Forward click from embedded row widgets to the list selection.
        try:
            if getattr(event, "type", None) and event.type() == event.Type.MouseButtonPress:  # type: ignore[attr-defined]
                pid = self._profile_row_widget_to_id.get(id(watched))
                if pid:
                    self._select_profile(pid)
                    return True
        except Exception:
            # Best-effort; never break UI interaction.
            return False
        return super().eventFilter(watched, event)

    def _load_active_profile_into_form(self) -> None:
        p = self._active_profile()
        if not p:
            return
        self.ed_name.setText(p.name)
        self.ed_proxy_server.setText(p.proxy_server or "")
        self.ed_proxy_user.setText(p.proxy_username or "")
        self.ed_proxy_pass.setText(p.proxy_password or "")
        self.ed_ua.setText(p.user_agent or "")
        self.ed_locale.setText(p.locale or "")
        self.ed_tz.setText(p.timezone_id or "")
        self.ed_country.setText(p.country_code or "")
        self._set_combo_by_data(self.cb_color, p.color_scheme)
        self.sp_lat.setValue(float(p.geo_lat or 0.0))
        self.sp_lon.setValue(float(p.geo_lon or 0.0))
        self.ed_webgl_vendor.setText(p.webgl_vendor or "")
        self.ed_webgl_renderer.setText(p.webgl_renderer or "")
        self.ed_webgl_version.setText(p.webgl_version or "")
        self.ed_webgl_slv.setText(p.webgl_shading_language_version or "")
        self._sync_locale_tz_system_mode()

    def _sync_locale_tz_system_mode(self) -> None:
        """
        If no proxy is set, locale/timezone are taken from system defaults:
        - UI shows blank values
        - fields are disabled (read-only)
        """
        no_proxy = not (self.ed_proxy_server.text() or "").strip()
        self.ed_locale.setEnabled(not no_proxy)
        self.ed_tz.setEnabled(not no_proxy)
        if no_proxy:
            # keep system defaults by clearing explicit overrides
            if self.ed_locale.text():
                self.ed_locale.setText("")
            if self.ed_tz.text():
                self.ed_tz.setText("")

    def _on_proxy_fields_edited(self) -> None:
        """
        When proxy changes, regenerate fingerprint/persona fields in the form.
        Changes are not persisted until user clicks 'Save profile'.
        """
        self._sync_locale_tz_system_mode()
        p = self._active_profile()
        if not p:
            return

        proxy_server = (self.ed_proxy_server.text() or "").strip() or None
        proxy_user = (self.ed_proxy_user.text() or "").strip() or None
        proxy_pass = (self.ed_proxy_pass.text() or "").strip() or None

        if (
            (p.proxy_server or None) == proxy_server
            and (p.proxy_username or None) == proxy_user
            and (p.proxy_password or None) == proxy_pass
        ):
            return

        # If proxy cleared: keep locale/tz system-default and don't regenerate persona.
        if not proxy_server:
            return

        base = replace(
            p,
            proxy_server=proxy_server,
            proxy_username=proxy_user,
            proxy_password=proxy_pass,
            # Always keep system-default sizing.
            viewport_width=None,
            viewport_height=None,
        )
        regen = generate_test_fingerprint(base)
        # Geo/timezone/country should match proxy. Best-effort.
        proxy_ip = get_proxy_ip(proxy_server, proxy_user, proxy_pass)
        if proxy_ip:
            geo = geoip_from_ip(proxy_ip)
        else:
            geo = None

        if geo:
            regen = replace(
                regen,
                country_code=str(geo.get("country_code") or "").strip().upper() or None,
                timezone_id=str(geo.get("timezone_id") or "").strip() or None,
                # Force locale to be derived from proxy country (avoid keeping random locale from generator).
                locale=None,
                geo_lat=geo.get("geo_lat") if geo.get("geo_lat") is not None else regen.geo_lat,
                geo_lon=geo.get("geo_lon") if geo.get("geo_lon") is not None else regen.geo_lon,
            )

        # Fill locale from country presets if missing (or keep regen locale if already coherent).
        regen = normalize_timezone_country(regen)
        regen = replace(regen, viewport_width=None, viewport_height=None)

        # Update form fields (but don't save yet).
        self.ed_ua.setText(regen.user_agent or "")
        self.ed_locale.setText(regen.locale or "")
        self.ed_tz.setText(regen.timezone_id or "")
        self.ed_country.setText(regen.country_code or "")
        self._set_combo_by_data(self.cb_color, regen.color_scheme)
        self.ed_webgl_vendor.setText(regen.webgl_vendor or "")
        self.ed_webgl_renderer.setText(regen.webgl_renderer or "")
        self.ed_webgl_version.setText(regen.webgl_version or "")
        self.ed_webgl_slv.setText(regen.webgl_shading_language_version or "")

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
        proxy_server = self._blank_to_none(self.ed_proxy_server.text())
        no_proxy = not proxy_server
        updated = replace(
            p,
            name=self.ed_name.text().strip() or p.name,
            proxy_server=proxy_server,
            proxy_username=self._blank_to_none(self.ed_proxy_user.text()),
            proxy_password=self._blank_to_none(self.ed_proxy_pass.text()),
            engine="chromium",
            device_preset=None,
            user_agent=self._blank_to_none(self.ed_ua.text()),
            # If no proxy is set, keep system-default locale/timezone.
            locale=None if no_proxy else self._blank_to_none(self.ed_locale.text()),
            timezone_id=None if no_proxy else self._blank_to_none(self.ed_tz.text()),
            country_code=self._blank_to_none(self.ed_country.text()),
            color_scheme=self.cb_color.currentData(),
            # Window/viewport size is system-default and not user-configurable in UI.
            viewport_width=None,
            viewport_height=None,
            geo_lat=self._geo_or_none(self.sp_lat.value()),
            geo_lon=self._geo_or_none(self.sp_lon.value()),
            webgl_vendor=self._blank_to_none(self.ed_webgl_vendor.text()),
            webgl_renderer=self._blank_to_none(self.ed_webgl_renderer.text()),
            webgl_version=self._blank_to_none(self.ed_webgl_version.text()),
            webgl_shading_language_version=self._blank_to_none(self.ed_webgl_slv.text()),
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

    def _launch_selected_from_profiles_list(self) -> None:
        ids: list[str] = []
        for it in self.profiles_list.selectedItems():
            pid = it.data(Qt.ItemDataRole.UserRole)
            if pid:
                ids.append(str(pid))
        if not ids:
            QMessageBox.information(self, "Выбор профилей", "Выделите один или несколько профилей для запуска.")
            return
        self._launch_profiles(ids)

    def _launch_all(self) -> None:
        ids = [p.profile_id for p in self._profiles]
        if not ids:
            QMessageBox.information(self, "Нет профилей", "Сначала создайте профиль.")
            return
        self._launch_profiles(ids)

    def _launch_profiles(self, ids: list[str]) -> None:
        url = self.ed_url.text().strip() or "https://2ip.ru"
        script = None

        for profile_id in ids:
            if profile_id in self._runners and self._runners[profile_id].isRunning():
                self._append_log(f"[{profile_id}] already running — skip")
                self._sync_run_button(profile_id)
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
            self._sync_run_button(profile_id)

    def _on_runner_finished(self, profile_id: str, prefix: str, ok: bool, msg: str) -> None:
        self._append_log(f"{prefix} finished: {'OK' if ok else 'FAIL'} — {msg}")
        r = self._runners.get(profile_id)
        if r and not r.isRunning():
            self._runners.pop(profile_id, None)
        self._sync_run_button(profile_id)

    def _append_log(self, s: str) -> None:
        self.log.appendPlainText(s)


def run_qt() -> None:
    app = QApplication([])
    app.setApplicationName("Antidetect UI")
    app.setStyleSheet(ZALIVER_DARK_QSS)
    w = MainWindow()
    # Open maximized ("full screen" for typical desktop usage).
    w.showMaximized()
    app.exec()

