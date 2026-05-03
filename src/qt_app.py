from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QDoubleSpinBox,
    QSizePolicy,
)

from profiles_store import BrowserProfile, load_profiles, save_profiles
from fingerprint_generator import generate_test_fingerprint
from proxy_health import probe_proxy_health_triple, update_all_profiles_matching_proxy_credentials
from proxy_import import apply_proxy_and_sync_geo, parse_host_port_user_pass_line, proxy_server_url
from playwright_runner import run_profile, profile_user_data_dir, get_proxy_ip, geoip_from_ip
from api_server import (
    append_ui_session_log,
    apply_ui_session_cdp,
    is_profile_running_via_api,
    notify_ui_session_finished,
    register_ui_session,
    request_stop_by_profile_id,
    set_api_ui_hooks,
    start_profile_api_background,
)
from fingerprint_consistency import normalize_timezone_country
from zaliver_theme import ZALIVER_DARK_QSS


class RunnerThread(QThread):
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(
        self,
        profile: BrowserProfile,
        start_url: str,
        script_path: Optional[str],
        *,
        tracked_session_id: Optional[str] = None,
        headless: bool = False,
        cdp_debug_port: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._profile = profile
        self._start_url = start_url
        self._script_path = script_path
        self._stop_evt = threading.Event()
        self._tracked_session_id = tracked_session_id
        self._headless = headless
        self._cdp_debug_port = cdp_debug_port

    def request_stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        tid = self._tracked_session_id

        def _log(line: str) -> None:
            if tid:
                append_ui_session_log(tid, line)
            self.log_line.emit(line)

        def _on_cdp(info: dict[str, object]) -> None:
            if tid:
                apply_ui_session_cdp(tid, info)

        res = run_profile(
            self._profile,
            start_url=self._start_url,
            script_path=self._script_path,
            log=_log,
            stop_requested=self._stop_evt.is_set,
            headless=self._headless,
            cdp_debug_port=self._cdp_debug_port,
            on_cdp_ready=_on_cdp if self._cdp_debug_port is not None else None,
        )
        self.finished_ok.emit(res.ok, res.message)


class ApiUiBridge(QObject):
    """Проброс колбэков из потоков API/uvicorn в GUI через очередь Qt."""

    log_line = pyqtSignal(str)
    sync_profile_run_button = pyqtSignal(str)

    def __init__(self, main: "MainWindow") -> None:
        super().__init__(main)
        self.log_line.connect(main._append_log)
        self.sync_profile_run_button.connect(main._sync_run_button)


class ProxyHealthCheckThread(QThread):
    """Один запрос проверки; результат записывается во все профили с тем же прокси."""

    finished_for_profile = pyqtSignal(str, bool, str, str)  # representative_profile_id, ok, msg, ts_utc

    def __init__(self, representative_profile_id: str, srv: str, user: str | None, password: str | None) -> None:
        super().__init__()
        self._rid = representative_profile_id
        self._srv = srv
        self._user = user
        self._password = password

    def run(self) -> None:
        ok, msg, ts = probe_proxy_health_triple(self._srv, self._user, self._password)
        self.finished_for_profile.emit(self._rid, ok, msg, ts)


class BatchImportProxyHealthThread(QThread):
    progress = pyqtSignal(int, int)
    finished_payload = pyqtSignal(dict)  # profile_id -> (ok, msg, ts)

    def __init__(self, jobs: list[tuple[str, str, str | None, str | None]]) -> None:
        super().__init__()
        self._jobs = jobs

    def run(self) -> None:
        payload: dict[str, tuple[bool, str, str]] = {}
        n = len(self._jobs)
        if n:
            self.progress.emit(0, n)
        for i, (pid, srv, u, pw) in enumerate(self._jobs):
            ok, msg, ts = probe_proxy_health_triple(srv, u, pw)
            payload[pid] = (ok, msg, ts)
            self.progress.emit(i + 1, n)
        self.finished_payload.emit(payload)


class ProxyBatchCheckProgressDialog(QDialog):
    """Явный прогресс-бар: импорт из файла, создание профилей, пакетная проверка прокси."""

    def __init__(
        self,
        parent: QWidget | None,
        total: int,
        *,
        window_title: str = "Проверка прокси",
        progress_caption: str = "Проверка прокси",
    ) -> None:
        super().__init__(parent)
        self._caption = progress_caption
        self.setWindowTitle(window_title)
        self.setModal(True)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        self._label = QLabel("Подготовка…")
        self._label.setWordWrap(True)
        self._bar = QProgressBar()
        self._bar.setRange(0, max(1, total))
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%p%")
        lay.addWidget(self._label)
        lay.addWidget(self._bar)

    def set_progress(self, current: int, total: int) -> None:
        t = max(1, total)
        c = max(0, min(current, t))
        self._bar.setMaximum(t)
        self._bar.setValue(c)
        self._label.setText(f"{self._caption}: {c} из {t}")


class ProxyStatusTableItem(QTableWidgetItem):
    """Сортировка по колонке статуса: рабочие → нерабочие → не проверены (по возрастанию ключа)."""

    def __init__(self, display: str, sort_key: int) -> None:
        super().__init__(display)
        self.setData(Qt.ItemDataRole.UserRole, sort_key)

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        if not isinstance(other, QTableWidgetItem):
            return False
        a, b = self.data(Qt.ItemDataRole.UserRole), other.data(Qt.ItemDataRole.UserRole)
        if a is not None and b is not None:
            try:
                return int(a) < int(b)
            except (TypeError, ValueError):
                pass
        return super().__lt__(other)


class ImportProfilesBuildThread(QThread):
    """Создание профилей из уже распарсенных строк (сеть в apply_proxy_and_sync_geo)."""

    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(object)  # list[BrowserProfile]
    failed = pyqtSignal(str)

    def __init__(
        self,
        valid_rows: list[tuple[str, str, str, str]],
        scheme: str,
        base_profile_count: int,
        existing_profile_ids: set[str],
    ) -> None:
        super().__init__()
        self._rows = valid_rows
        self._scheme = scheme
        self._base_profile_count = base_profile_count
        self._existing_ids = set(existing_profile_ids)

    def run(self) -> None:
        try:
            n = len(self._rows)
            if n == 0:
                self.finished_ok.emit([])
                return
            self.progress.emit(0, n)
            local_ids = set(self._existing_ids)
            created: list[BrowserProfile] = []
            for i, (host, port, user, pwd) in enumerate(self._rows):
                server = proxy_server_url(host, port, self._scheme)
                new_id = uuid.uuid4().hex[:12]
                while new_id in local_ids:
                    new_id = uuid.uuid4().hex[:12]
                local_ids.add(new_id)
                idx = self._base_profile_count + len(created) + 1
                base = BrowserProfile(profile_id=new_id, name=f"Profile {idx}")
                p = generate_test_fingerprint(base)
                p = apply_proxy_and_sync_geo(p, proxy_server=server, proxy_username=user, proxy_password=pwd)
                created.append(p)
                self.progress.emit(i + 1, n)
            self.finished_ok.emit(created)
        except Exception as e:
            self.failed.emit(str(e).strip() or "Ошибка при создании профилей")


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
        self._proxy_health_thread: ProxyHealthCheckThread | None = None
        self._import_health_thread: BatchImportProxyHealthThread | None = None
        self._import_health_dialog: ProxyBatchCheckProgressDialog | None = None
        self._import_build_thread: ImportProfilesBuildThread | None = None
        self._proxy_single_check_dialog: QProgressDialog | None = None

        layout = QHBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # left nav
        nav = QListWidget()
        nav.setObjectName("sideNav")
        nav.addItem("Профили")
        nav.addItem("Прокси")
        nav.setFixedWidth(180)
        nav.setCurrentRow(0)

        # pages
        self.pages = QStackedWidget()
        self.page_profiles = self._build_profiles_page()
        self.page_proxies = self._build_proxies_page()
        self.pages.addWidget(self.page_profiles)
        self.pages.addWidget(self.page_proxies)

        self._api_bridge = ApiUiBridge(self)
        set_api_ui_hooks(
            log_line=self._api_bridge.log_line.emit,
            sync_profile_button=self._api_bridge.sync_profile_run_button.emit,
            is_profile_running_in_ui=self._is_runner_thread_active,
        )

        splitter.addWidget(nav)
        splitter.addWidget(self.pages)
        splitter.setStretchFactor(1, 1)

        nav.currentRowChanged.connect(self._on_nav_changed)

        self._apply_theme()
        self._setup_proxy_refresh_icon()
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
        self.btn_import_proxies = QPushButton("Из файла…")
        self.btn_import_proxies.setObjectName("secondary")
        self.btn_import_proxies.setToolTip("Текстовый файл: по одной строке host:port:user:pass")
        self.btn_delete = QPushButton("Удалить")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.setToolTip("Удалить все выделенные профили; если ничего не выделено — текущий открытый в форме")
        btn_row.addWidget(self.btn_new)
        btn_row.addWidget(self.btn_import_proxies)
        btn_row.addWidget(self.btn_delete)
        list_layout.addLayout(btn_row)

        self.btn_new.clicked.connect(self._create_profile)
        self.btn_import_proxies.clicked.connect(self._import_profiles_from_proxy_file)
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

        self.lbl_proxy_health = QLabel("")
        self.lbl_proxy_health.setFixedWidth(22)
        self.lbl_proxy_health.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_proxy_health_refresh = QPushButton()
        self.btn_proxy_health_refresh.setObjectName("secondary")
        self.btn_proxy_health_refresh.setFixedSize(36, 30)
        self.btn_proxy_health_refresh.setToolTip("Проверить сохранённый прокси (по данным из профиля на диске)")
        self.btn_proxy_health_refresh.clicked.connect(self._on_click_proxy_health_refresh)

        self._proxy_server_row = QWidget()
        pr_l = QHBoxLayout(self._proxy_server_row)
        pr_l.setContentsMargins(0, 0, 0, 0)
        pr_l.setSpacing(8)
        pr_l.addWidget(self.ed_proxy_server, 1)
        pr_l.addWidget(self.lbl_proxy_health, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pr_l.addWidget(self.btn_proxy_health_refresh, 0)

        # If proxy changes, regenerate persona (but don't auto-save).
        self.ed_proxy_server.editingFinished.connect(self._on_proxy_fields_edited)
        self.ed_proxy_user.editingFinished.connect(self._on_proxy_fields_edited)
        self.ed_proxy_pass.editingFinished.connect(self._on_proxy_fields_edited)
        # Live-toggle locale/tz behavior when proxy becomes empty/non-empty.
        self.ed_proxy_server.textChanged.connect(lambda _t: self._sync_locale_tz_system_mode())
        self.ed_proxy_server.textChanged.connect(lambda _t: self._sync_proxy_health_badge())
        self.ed_proxy_user.textChanged.connect(lambda _t: self._sync_proxy_health_badge())
        self.ed_proxy_pass.textChanged.connect(lambda _t: self._sync_proxy_health_badge())

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
        form.addRow("Прокси (сервер)", self._proxy_server_row)
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

    def _setup_proxy_refresh_icon(self) -> None:
        self.btn_proxy_health_refresh.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))

    def _on_nav_changed(self, row: int) -> None:
        self.pages.setCurrentIndex(row)
        if row == 1:
            self._refresh_proxies_page_table()

    def _build_proxies_page(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(12)
        title = QLabel("Прокси")
        title.setObjectName("title")
        l.addWidget(title)
        hint = QLabel(
            "Сводка по всем прокси из профилей. Статус обновляется при импорте списка, "
            "по кнопке обновления здесь или у поля «Прокси (сервер)» в карточке профиля — не пересчитывается при каждом редактировании."
        )
        hint.setWordWrap(True)
        l.addWidget(hint)

        self.table_proxies = QTableWidget(0, 6)
        self.table_proxies.setObjectName("proxiesTable")
        self.table_proxies.setHorizontalHeaderLabels(
            ["Сервер", "Логин", "Статус", "Проверено", "Профилей", ""],
        )
        self.table_proxies.verticalHeader().setVisible(False)
        self.table_proxies.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_proxies.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_proxies.setSortingEnabled(True)
        hh = self.table_proxies.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table_proxies.setColumnWidth(5, 44)
        l.addWidget(self.table_proxies, 1)
        return w

    def _group_profiles_by_proxy(self) -> dict[tuple[str, str | None, str | None], list[BrowserProfile]]:
        groups: dict[tuple[str, str | None, str | None], list[BrowserProfile]] = {}
        for p in self._profiles:
            srv = (p.proxy_server or "").strip()
            if not srv:
                continue
            u = (p.proxy_username or "").strip() or None
            pw = (p.proxy_password or "").strip() or None
            key = (srv, u, pw)
            groups.setdefault(key, []).append(p)
        return groups

    def _newest_health_in_group(self, members: list[BrowserProfile]) -> tuple[bool | None, str | None, str | None]:
        with_ts = [m for m in members if m.proxy_health_checked_at]
        if not with_ts:
            return None, None, None
        best = max(with_ts, key=lambda m: (m.proxy_health_checked_at or ""))
        return best.proxy_health_ok, best.proxy_health_checked_at, best.proxy_health_message

    def _refresh_proxies_page_table(self) -> None:
        if not hasattr(self, "table_proxies"):
            return
        groups = self._group_profiles_by_proxy()
        self.table_proxies.setSortingEnabled(False)
        self.table_proxies.setRowCount(0)
        for key in sorted(groups.keys(), key=lambda k: k[0].lower()):
            members = groups[key]
            srv, u, _pw = key
            row = self.table_proxies.rowCount()
            self.table_proxies.insertRow(row)
            rep = members[0]
            ok, ts, msg = self._newest_health_in_group(members)
            if ok is True:
                status_label, sort_key = "Рабочий", 0
            elif ok is False:
                status_label, sort_key = "Нерабочий", 1
            else:
                status_label, sort_key = "Не проверен", 2
            tip = (msg or "").strip()
            if len(tip) > 900:
                tip = tip[:900] + "…"
            st_item = ProxyStatusTableItem(status_label, sort_key)
            st_item.setToolTip(tip if tip else status_label)
            self.table_proxies.setItem(row, 0, QTableWidgetItem(srv))
            self.table_proxies.setItem(row, 1, QTableWidgetItem(u or "—"))
            self.table_proxies.setItem(row, 2, st_item)
            self.table_proxies.setItem(row, 3, QTableWidgetItem(ts or "—"))
            self.table_proxies.setItem(row, 4, QTableWidgetItem(str(len(members))))
            btn = QPushButton()
            btn.setObjectName("secondary")
            btn.setFixedSize(36, 30)
            btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            btn.setToolTip("Проверить этот прокси")
            btn.clicked.connect(lambda _c=False, pid=rep.profile_id: self._on_proxies_table_refresh_click(pid))
            self.table_proxies.setCellWidget(row, 5, btn)
        self.table_proxies.setSortingEnabled(True)
        self.table_proxies.sortItems(2, Qt.SortOrder.AscendingOrder)

    def _on_proxies_table_refresh_click(self, representative_profile_id: str) -> None:
        self._start_proxy_health_check(representative_profile_id)

    def _proxy_form_matches_saved(self) -> bool:
        p = self._active_profile()
        if not p:
            return False
        return (
            (p.proxy_server or "").strip() == (self.ed_proxy_server.text() or "").strip()
            and (p.proxy_username or "").strip() == (self.ed_proxy_user.text() or "").strip()
            and (p.proxy_password or "").strip() == (self.ed_proxy_pass.text() or "").strip()
        )

    def _sync_proxy_health_badge(self) -> None:
        if not hasattr(self, "lbl_proxy_health"):
            return
        p = self._active_profile()
        if not p or not (p.proxy_server or "").strip():
            self.lbl_proxy_health.setText("")
            self.lbl_proxy_health.setToolTip("")
            self.lbl_proxy_health.setStyleSheet("")
            self.btn_proxy_health_refresh.setEnabled(False)
            return
        self.btn_proxy_health_refresh.setEnabled(True)
        if self._proxy_health_thread and self._proxy_health_thread.isRunning():
            self.lbl_proxy_health.setText("…")
            self.lbl_proxy_health.setStyleSheet("color: #aaa;")
            self.lbl_proxy_health.setToolTip("Проверка…")
            self.btn_proxy_health_refresh.setEnabled(False)
            return
        if not self._proxy_form_matches_saved():
            self.lbl_proxy_health.setText("●")
            self.lbl_proxy_health.setStyleSheet("color: #aaa;")
            self.lbl_proxy_health.setToolTip(
                "Поля прокси в форме не совпадают с сохранёнными — сохраните профиль, чтобы статус соответствовал данным на диске."
            )
            return
        if p.proxy_health_ok is True:
            self.lbl_proxy_health.setText("●")
            self.lbl_proxy_health.setStyleSheet("color: #6c6;")
            tip = p.proxy_health_message or "OK"
        elif p.proxy_health_ok is False:
            self.lbl_proxy_health.setText("●")
            self.lbl_proxy_health.setStyleSheet("color: #c66;")
            tip = p.proxy_health_message or "Ошибка"
        else:
            self.lbl_proxy_health.setText("●")
            self.lbl_proxy_health.setStyleSheet("color: #888;")
            tip = "Проверка ещё не выполнялась"
        if p.proxy_health_checked_at:
            tip = f"{tip}\n{p.proxy_health_checked_at}"
        self.lbl_proxy_health.setToolTip(tip)

    def _on_click_proxy_health_refresh(self) -> None:
        p = self._active_profile()
        if not p or not (p.proxy_server or "").strip():
            QMessageBox.information(self, "Прокси", "У профиля не задан прокси.")
            return
        if not self._proxy_form_matches_saved():
            QMessageBox.information(
                self,
                "Прокси",
                "Сохраните профиль, чтобы поля прокси совпадали с сохранёнными, затем нажмите проверку.",
            )
            return
        self._start_proxy_health_check(p.profile_id)

    def _start_proxy_health_check(self, representative_profile_id: str) -> None:
        if self._proxy_health_thread and self._proxy_health_thread.isRunning():
            return
        rep = next((x for x in self._profiles if x.profile_id == representative_profile_id), None)
        if not rep or not (rep.proxy_server or "").strip():
            return
        spd = QProgressDialog("Проверка прокси через сеть…", "", 0, 0, self)
        spd.setWindowTitle("Прокси")
        spd.setWindowModality(Qt.WindowModality.ApplicationModal)
        spd.setMinimumDuration(0)
        spd.setMinimumWidth(400)
        spd.setCancelButton(None)
        spd.setRange(0, 0)
        spd.show()
        QApplication.processEvents()
        self._proxy_single_check_dialog = spd

        self._proxy_health_thread = ProxyHealthCheckThread(
            representative_profile_id,
            rep.proxy_server or "",
            rep.proxy_username,
            rep.proxy_password,
        )

        def _finish_single(rep_id: str, ok: bool, msg: str, ts: str) -> None:
            if self._proxy_single_check_dialog:
                self._proxy_single_check_dialog.hide()
                self._proxy_single_check_dialog.deleteLater()
                self._proxy_single_check_dialog = None
            self._on_proxy_health_check_finished(rep_id, ok, msg, ts)

        self._proxy_health_thread.finished_for_profile.connect(_finish_single)
        self._proxy_health_thread.finished.connect(self._on_proxy_health_check_thread_cleanup)
        self._sync_proxy_health_badge()
        self._proxy_health_thread.start()

    def _on_proxy_health_check_thread_cleanup(self) -> None:
        self._proxy_health_thread = None
        self._sync_proxy_health_badge()

    def _on_proxy_health_check_finished(self, rep_id: str, ok: bool, msg: str, ts: str) -> None:
        rep = next((x for x in self._profiles if x.profile_id == rep_id), None)
        if not rep:
            return
        self._profiles = update_all_profiles_matching_proxy_credentials(
            self._profiles,
            proxy_server=rep.proxy_server or "",
            proxy_username=rep.proxy_username,
            proxy_password=rep.proxy_password,
            ok=ok,
            message=msg,
            checked_at=ts,
        )
        save_profiles(self._profiles)
        self._refresh_profiles_list()
        self._load_active_profile_into_form()
        self._refresh_proxies_page_table()

    def _apply_batch_import_health_payload(self, payload: dict) -> None:
        pmap = {pid: (ok, msg, ts) for pid, (ok, msg, ts) in payload.items()}
        new_list: list[BrowserProfile] = []
        for p in self._profiles:
            if p.profile_id in pmap:
                ok, msg, ts = pmap[p.profile_id]
                new_list.append(
                    replace(p, proxy_health_ok=ok, proxy_health_checked_at=ts, proxy_health_message=msg)
                )
            else:
                new_list.append(p)
        self._profiles = new_list
        save_profiles(self._profiles)
        self._refresh_profiles_list()
        self._load_active_profile_into_form()
        self._refresh_proxies_page_table()

    def _start_batch_import_health(
        self,
        jobs: list[tuple[str, str, str | None, str | None]],
        *,
        created_count: int,
        skipped_extra: str,
    ) -> None:
        if not jobs:
            QMessageBox.information(self, "Импорт", f"Создано профилей: {created_count}.{skipped_extra}")
            return
        if self._import_health_thread and self._import_health_thread.isRunning():
            return
        dlg = ProxyBatchCheckProgressDialog(
            self,
            len(jobs),
            window_title="Импорт",
            progress_caption="Проверка прокси",
        )
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._import_health_dialog = dlg
        dlg.show()
        QApplication.processEvents()

        self._import_health_thread = BatchImportProxyHealthThread(jobs)

        def on_prog(cur: int, total: int) -> None:
            dlg.set_progress(cur, total)
            QApplication.processEvents()

        def on_done(payload: dict) -> None:
            dlg.hide()
            dlg.deleteLater()
            self._import_health_dialog = None
            self._import_health_thread = None
            self._apply_batch_import_health_payload(payload)
            QMessageBox.information(
                self,
                "Импорт",
                f"Создано профилей: {created_count}. Проверка прокси завершена.{skipped_extra}",
            )

        self._import_health_thread.progress.connect(on_prog)
        self._import_health_thread.finished_payload.connect(on_done)
        self._import_health_thread.start()

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
        self._sync_proxy_health_badge()

    def _is_runner_thread_active(self, profile_id: str) -> bool:
        r = self._runners.get(profile_id)
        return bool(r and r.isRunning())

    def _is_profile_running(self, profile_id: str) -> bool:
        if self._is_runner_thread_active(profile_id):
            return True
        return is_profile_running_via_api(profile_id)

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
        if r and r.isRunning():
            self._append_log(f"[{profile_id}] stop requested")
            r.request_stop()
            self._sync_run_button(profile_id)
            return
        if is_profile_running_via_api(profile_id):
            if request_stop_by_profile_id(profile_id, from_ui=True):
                self._append_log(f"[{profile_id}] stop requested (сессия через API)")
            self._sync_run_button(profile_id)
            return
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
            self._sync_proxy_health_badge()
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
        self._sync_proxy_health_badge()

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

    def _import_profiles_from_proxy_file(self) -> None:
        if self._import_build_thread and self._import_build_thread.isRunning():
            QMessageBox.information(self, "Импорт", "Дождитесь завершения текущего импорта.")
            return
        if self._import_health_thread and self._import_health_thread.isRunning():
            QMessageBox.information(self, "Импорт", "Дождитесь завершения проверки прокси.")
            return
        path, _sel = QFileDialog.getOpenFileName(
            self,
            "Файл с прокси",
            "",
            "Текст (*.txt);;Все файлы (*.*)",
        )
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Импорт", f"Не удалось прочитать файл:\n{e}")
            return

        valid_rows: list[tuple[str, str, str, str]] = []
        skipped = 0
        for line in raw.splitlines():
            parsed = parse_host_port_user_pass_line(line)
            if parsed is not None:
                valid_rows.append(parsed)
            elif (line or "").strip():
                skipped += 1

        if not valid_rows:
            QMessageBox.information(
                self,
                "Импорт",
                "Не найдено ни одной строки формата host:port:user:pass.",
            )
            return

        scheme = "http"
        n = len(valid_rows)
        dlg = ProxyBatchCheckProgressDialog(
            self,
            n,
            window_title="Импорт из файла",
            progress_caption="Создание профилей",
        )
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.show()
        QApplication.processEvents()

        existing_ids = {p.profile_id for p in self._profiles}
        base_count = len(self._profiles)
        self._import_build_thread = ImportProfilesBuildThread(valid_rows, scheme, base_count, existing_ids)

        def on_prog(cur: int, total: int) -> None:
            dlg.set_progress(cur, total)
            QApplication.processEvents()

        def on_build_done(created_obj: object) -> None:
            self._import_build_thread = None
            dlg.hide()
            dlg.deleteLater()
            created = created_obj  # type: list[BrowserProfile]
            if not created:
                return
            self._profiles.extend(created)
            save_profiles(self._profiles)
            self._active_profile_id = created[-1].profile_id
            self._refresh_profiles_list()
            self._load_active_profile_into_form()
            extra = f"\n\nПропущено строк без нужного формата: {skipped}." if skipped else ""
            jobs = [
                (c.profile_id, c.proxy_server or "", c.proxy_username, c.proxy_password)
                for c in created
                if (c.proxy_server or "").strip()
            ]
            self._start_batch_import_health(jobs, created_count=len(created), skipped_extra=extra)

        def on_build_failed(msg: str) -> None:
            self._import_build_thread = None
            dlg.hide()
            dlg.deleteLater()
            QMessageBox.warning(self, "Импорт", msg)

        self._import_build_thread.progress.connect(on_prog)
        self._import_build_thread.finished_ok.connect(on_build_done)
        self._import_build_thread.failed.connect(on_build_failed)
        self._import_build_thread.start()

    def _selected_profile_ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for it in self.profiles_list.selectedItems():
            pid = it.data(Qt.ItemDataRole.UserRole)
            if not pid:
                continue
            s = str(pid)
            if s not in seen:
                seen.add(s)
                ids.append(s)
        return ids

    def _delete_profile(self) -> None:
        ids = self._selected_profile_ids()
        if not ids and self._active_profile_id:
            ids = [self._active_profile_id]
        if not ids:
            QMessageBox.information(self, "Удаление", "Нет профилей для удаления.")
            return

        to_remove: list[BrowserProfile] = [p for p in self._profiles if p.profile_id in ids]
        if not to_remove:
            return

        running = [p.profile_id for p in to_remove if self._is_profile_running(p.profile_id)]
        if running:
            QMessageBox.warning(
                self,
                "Запущен профиль",
                "Нельзя удалить, пока браузер запущен. Остановите или закройте окна для:\n"
                + "\n".join(running),
            )
            return

        if len(to_remove) == 1:
            p0 = to_remove[0]
            msg = f"Удалить профиль «{p0.name}»?\n\nID: {p0.profile_id}"
        else:
            msg = f"Удалить выбранные профили ({len(to_remove)} шт.)?\n\n" + "\n".join(
                f"• {p.name} ({p.profile_id})" for p in to_remove[:20]
            )
            if len(to_remove) > 20:
                msg += f"\n… и ещё {len(to_remove) - 20}"

        res = QMessageBox.question(self, "Удаление профилей", msg)
        if res != QMessageBox.StandardButton.Yes:
            return

        remove_ids = {p.profile_id for p in to_remove}
        for p in to_remove:
            try:
                shutil.rmtree(profile_user_data_dir(p.profile_id), ignore_errors=True)
            except Exception:
                pass

        self._profiles = [x for x in self._profiles if x.profile_id not in remove_ids]
        if self._active_profile_id in remove_ids:
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
        proxy_user = self._blank_to_none(self.ed_proxy_user.text())
        proxy_pass = self._blank_to_none(self.ed_proxy_pass.text())
        proxy_changed = (
            (p.proxy_server or None) != proxy_server
            or (p.proxy_username or None) != proxy_user
            or (p.proxy_password or None) != proxy_pass
        )
        updated = replace(
            p,
            name=self.ed_name.text().strip() or p.name,
            proxy_server=proxy_server,
            proxy_username=proxy_user,
            proxy_password=proxy_pass,
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
        if proxy_changed:
            updated = replace(
                updated,
                proxy_health_ok=None,
                proxy_health_checked_at=None,
                proxy_health_message=None,
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
            if is_profile_running_via_api(profile_id):
                self._append_log(f"[{profile_id}] уже запущен через API — пропуск")
                self._sync_run_button(profile_id)
                continue

            p = next((x for x in self._profiles if x.profile_id == profile_id), None)
            if not p:
                self._append_log(f"[{profile_id}] not found — skip")
                continue

            prefix = f"[{p.name}:{p.profile_id}]"
            self._append_log(f"{prefix} launch")
            sid, ui_cdp = register_ui_session(
                profile_id,
                lambda pid=profile_id: self._stop_profile(pid),
                headless=False,
                start_url=url,
                script_path=script,
                expose_cdp=True,
            )
            runner = RunnerThread(
                p,
                url,
                script,
                tracked_session_id=sid,
                headless=False,
                cdp_debug_port=ui_cdp,
            )
            runner.log_line.connect(lambda s, pref=prefix: self._append_log(f"{pref} {s}"))
            runner.finished_ok.connect(lambda ok, msg, pref=prefix, pid=profile_id: self._on_runner_finished(pid, pref, ok, msg))
            setattr(runner, "api_tracked_session_id", sid)
            self._runners[profile_id] = runner
            runner.start()
            self._sync_run_button(profile_id)

    def _on_runner_finished(self, profile_id: str, prefix: str, ok: bool, msg: str) -> None:
        self._append_log(f"{prefix} finished: {'OK' if ok else 'FAIL'} — {msg}")
        r = self._runners.get(profile_id)
        tid = getattr(r, "api_tracked_session_id", None) if r else None
        if r and not r.isRunning():
            self._runners.pop(profile_id, None)
        if isinstance(tid, str) and tid:
            notify_ui_session_finished(tid, ok, msg)
        self._sync_run_button(profile_id)

    def _append_log(self, s: str) -> None:
        self.log.appendPlainText(s)


def run_qt() -> None:
    app = QApplication([])
    app.setApplicationName("Antidetect UI")
    app.setStyleSheet(ZALIVER_DARK_QSS)
    w = MainWindow()
    base = start_profile_api_background()
    if base:
        w._append_log(f"[API] локальный сервер: {base}/docs")
    # Open maximized ("full screen" for typical desktop usage).
    w.showMaximized()
    app.exec()

