from __future__ import annotations

import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QEvent, QPoint, QItemSelectionModel, QObject, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QCursor, QFont, QFontMetrics, QMouseEvent, QTextDocument, QTextOption
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
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
    QRadioButton,
    QScrollBar,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
    QDoubleSpinBox,
    QSizePolicy,
)

from profiles_store import (
    BrowserProfile,
    count_legacy_json_profiles,
    custom_data_from_json_text,
    custom_data_to_json_text,
    legacy_json_path,
    load_profiles,
    migrate_json_to_sqlite,
    needs_json_migration,
    save_profiles,
    tags_from_delimited_text,
)
from fingerprint_generator import generate_test_fingerprint, regenerate_profile_fingerprint
from proxy_health import probe_proxy_health_triple, update_all_profiles_matching_proxy_credentials
from proxy_import import apply_proxy_and_sync_geo, parse_host_port_user_pass_line, proxy_server_url
from playwright_runner import (
    run_profile,
    profile_user_data_dir,
    get_proxy_ip,
    geoip_from_ip,
    normalize_proxy_server_url,
    canonical_proxy_key,
)
from api_server import (
    append_ui_session_log,
    apply_ui_session_cdp,
    is_profile_running_via_api,
    notify_ui_session_finished,
    register_ui_session,
    request_stop_by_profile_id,
    set_api_ui_hooks,
    set_ui_profile_running,
    start_profile_api_background,
)
from fingerprint_consistency import normalize_timezone_country
from zaliver_theme import ZALIVER_DARK_QSS
from app_icon import build_app_icon


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
    sync_profile_metadata = pyqtSignal(str)

    def __init__(self, main: "MainWindow") -> None:
        super().__init__(main)
        self.log_line.connect(main._append_log)
        self.sync_profile_run_button.connect(main._sync_run_button)
        self.sync_profile_metadata.connect(main._sync_profile_from_disk)


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


PROXY_HEALTH_BATCH_MAX_WORKERS = 12

# Колонки таблицы на вкладке «Прокси»
_PROXY_COL_SERVER = 0
_PROXY_COL_LOGIN = 1
_PROXY_COL_PASSWORD = 2
_PROXY_COL_IDS = 3
_PROXY_COL_STATUS = 4
_PROXY_COL_CHECKED = 5
_PROXY_COL_COUNT = 6
_PROXY_COL_REFRESH = 7


class BatchImportProxyHealthThread(QThread):
    progress = pyqtSignal(int, int)
    finished_payload = pyqtSignal(dict)  # profile_id -> (ok, msg, ts)

    def __init__(
        self,
        jobs: list[tuple[str, str, str | None, str | None]],
        *,
        max_workers: int | None = None,
    ) -> None:
        super().__init__()
        self._jobs = jobs
        self._max_workers = max_workers

    def run(self) -> None:
        payload: dict[str, tuple[bool, str, str]] = {}
        n = len(self._jobs)
        if not n:
            self.finished_payload.emit(payload)
            return
        self.progress.emit(0, n)
        workers = self._max_workers
        if workers is None:
            workers = min(PROXY_HEALTH_BATCH_MAX_WORKERS, n)

        def _probe_job(job: tuple[str, str, str | None, str | None]) -> tuple[str, bool, str, str]:
            pid, srv, u, pw = job
            ok, msg, ts = probe_proxy_health_triple(srv, u, pw)
            return pid, ok, msg, ts

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_probe_job, job) for job in self._jobs]
            for done, fut in enumerate(as_completed(futures), start=1):
                pid, ok, msg, ts = fut.result()
                payload[pid] = (ok, msg, ts)
                self.progress.emit(done, n)
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


class _ClearProfilesOptionsDialog(QDialog):
    """Первый шаг очистки: выбор действий и кнопка «Далее»."""

    def __init__(self, parent: QWidget | None, message: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Очистка профилей")
        self.setModal(True)
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        lbl = QLabel(message)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self.cb_fingerprint = QCheckBox("Сменить отпечаток")
        self.cb_browser_data = QCheckBox("Очистить все данные в браузере")
        lay.addWidget(self.cb_fingerprint)
        lay.addWidget(self.cb_browser_data)

        hint = QLabel("Выберите хотя бы одно действие.")
        hint.setObjectName("hint")
        lay.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Отмена")
        self.btn_next = QPushButton("Далее")
        self.btn_next.setDefault(True)
        self.btn_next.setEnabled(False)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self.btn_next)
        lay.addLayout(btn_row)

        self.cb_fingerprint.toggled.connect(self._sync_next_enabled)
        self.cb_browser_data.toggled.connect(self._sync_next_enabled)
        btn_cancel.clicked.connect(self.reject)
        self.btn_next.clicked.connect(self._on_next)

    def _sync_next_enabled(self) -> None:
        self.btn_next.setEnabled(self.cb_fingerprint.isChecked() or self.cb_browser_data.isChecked())

    def _on_next(self) -> None:
        if self.cb_fingerprint.isChecked() or self.cb_browser_data.isChecked():
            self.accept()

    @property
    def change_fingerprint(self) -> bool:
        return self.cb_fingerprint.isChecked()

    @property
    def clear_browser_data(self) -> bool:
        return self.cb_browser_data.isChecked()


class ExportProfilesOptionsDialog(QDialog):
    """Выбор режима экспорта: полный архив или только cookies."""

    def __init__(self, parent: QWidget | None, profile_count: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Экспорт профилей")
        self.setModal(True)
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        lbl = QLabel(
            f"Экспортировать {profile_count} профил(я/ей). "
            "Браузеры должны быть закрыты."
        )
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self.rb_full = QRadioButton("Полный архив — весь user-data Chromium (тяжёлый)")
        self.rb_cookies = QRadioButton("Только cookies — лёгкий архив, выбор сайтов")
        self.rb_full.setChecked(True)
        lay.addWidget(self.rb_full)
        lay.addWidget(self.rb_cookies)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_ok = QPushButton("Далее")
        btn_ok.setDefault(True)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        lay.addLayout(btn_row)

        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self.accept)

    @property
    def cookies_only(self) -> bool:
        return self.rb_cookies.isChecked()


class CookieHostsSelectDialog(QDialog):
    """Выбор доменов для экспорта cookies."""

    def __init__(self, parent: QWidget | None, hosts: list[tuple[str, int]]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Сайты для экспорта cookies")
        self.setModal(True)
        self.setMinimumSize(520, 420)
        self._checks: list[tuple[str, QCheckBox]] = []

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        hint = QLabel("Отметьте домены, cookies которых попадут в архив.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_all = QPushButton("Выбрать все")
        btn_none = QPushButton("Снять все")
        btn_row.addWidget(btn_all)
        btn_row.addWidget(btn_none)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setSpacing(4)
        for host, count in hosts:
            cb = QCheckBox(f"{host}  ({count})")
            cb.setChecked(True)
            inner_lay.addWidget(cb)
            self._checks.append((host, cb))
        inner_lay.addStretch()
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_ok = QPushButton("Экспорт")
        btn_ok.setDefault(True)
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_ok)
        lay.addLayout(footer)

        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self.accept)

    def _set_all(self, checked: bool) -> None:
        for _, cb in self._checks:
            cb.setChecked(checked)

    def selected_hosts(self) -> set[str]:
        return {host for host, cb in self._checks if cb.isChecked()}


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


class ProxyPasswordDelegate(QStyledItemDelegate):
    """В ячейке — звёздочки; при редактировании — открытый текст пароля."""

    REAL_PASSWORD_ROLE = Qt.ItemDataRole.UserRole + 10

    @staticmethod
    def mask_password(password: str | None) -> str:
        n = len((password or "").strip())
        return "•" * n if n else ""

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index) -> QLineEdit:  # type: ignore[no-untyped-def]
        editor = QLineEdit(parent)
        editor.setEchoMode(QLineEdit.EchoMode.Normal)
        editor.setPlaceholderText("Пароль прокси")
        return editor

    def setEditorData(self, editor: QLineEdit, index) -> None:  # type: ignore[no-untyped-def]
        raw = index.data(self.REAL_PASSWORD_ROLE)
        editor.setText("" if raw is None else str(raw))

    def setModelData(self, editor: QLineEdit, model, index) -> None:  # type: ignore[no-untyped-def]
        password = editor.text()
        model.setData(index, self.mask_password(password), Qt.ItemDataRole.EditRole)
        model.setData(index, password, self.REAL_PASSWORD_ROLE)

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:  # type: ignore[no-untyped-def]
        super().initStyleOption(option, index)
        real = index.data(self.REAL_PASSWORD_ROLE)
        if real is not None:
            option.text = self.mask_password(str(real))


class _ProxyProfileIdChip(QFrame):
    """Один profile_id: клик по ID — открыть профиль, ⧉ — копировать ID."""

    def __init__(
        self,
        profile_id: str,
        profile_name: str,
        *,
        on_open: Callable[[str], None],
        on_copy: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("proxyProfileIdChip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 4, 4)
        lay.setSpacing(4)
        open_btn = QPushButton(profile_id)
        open_btn.setObjectName("proxyProfileIdOpenBtn")
        open_btn.setFlat(True)
        open_btn.setFont(QFont("Consolas", 9))
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setToolTip(
            f"{profile_id}\n{profile_name or 'без имени'}\n\nКлик — открыть профиль во вкладке «Профили»"
        )
        open_btn.clicked.connect(lambda _c=False, pid=profile_id: on_open(pid))
        copy_btn = QPushButton()
        copy_btn.setObjectName("proxyIdCopyBtn")
        copy_btn.setFixedSize(24, 24)
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_btn.setToolTip("Копировать ID в буфер")
        copy_btn.setText("⧉")
        copy_btn.setFont(QFont("Segoe UI Symbol", 10))
        copy_btn.clicked.connect(lambda _c=False, pid=profile_id: on_copy(pid))
        lay.addWidget(open_btn, 0)
        lay.addWidget(copy_btn, 0)


class _ProxyProfileIdsCell(QWidget):
    """Список profile_id в строке таблицы (без QScrollArea — стабильнее в QTableWidget)."""

    def __init__(
        self,
        members: list[BrowserProfile],
        *,
        on_open: Callable[[str], None],
        on_copy: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        ordered = sorted(members, key=lambda m: ((m.name or "").lower(), m.profile_id))
        self.setToolTip("\n".join(f"{p.profile_id} — {p.name or 'без имени'}" for p in ordered))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(4)
        row_w: QWidget | None = None
        row_l: QHBoxLayout | None = None
        per_row = 2
        for i, p in enumerate(ordered):
            if i % per_row == 0:
                row_w = QWidget(self)
                row_l = QHBoxLayout(row_w)
                row_l.setContentsMargins(0, 0, 0, 0)
                row_l.setSpacing(6)
                outer.addWidget(row_w)
            assert row_l is not None
            row_l.addWidget(
                _ProxyProfileIdChip(
                    p.profile_id,
                    p.name or "",
                    on_open=on_open,
                    on_copy=on_copy,
                    parent=row_w,
                ),
                0,
            )
        if row_l is not None:
            row_l.addStretch(1)


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


class ProfilesArchiveExportThread(QThread):
    """ZIP: полный user-data или лёгкий архив cookies."""

    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        dest_dir: str,
        profiles: list[BrowserProfile],
        *,
        cookies_only: bool = False,
        cookie_hosts: set[str] | None = None,
    ) -> None:
        super().__init__()
        self._dest_dir = dest_dir
        self._profiles = profiles
        self._cookies_only = cookies_only
        self._cookie_hosts = cookie_hosts

    def run(self) -> None:
        try:
            if self._cookies_only:
                from profiles_bundle import export_profiles_cookies_zip

                path = export_profiles_cookies_zip(
                    Path(self._dest_dir),
                    self._profiles,
                    self._cookie_hosts or set(),
                    progress=lambda s: self.progress.emit(s),
                )
            else:
                from profiles_bundle import export_profiles_zip

                path = export_profiles_zip(
                    Path(self._dest_dir),
                    self._profiles,
                    progress=lambda s: self.progress.emit(s),
                )
            self.finished_ok.emit(str(path))
        except Exception as e:
            self.failed.emit(str(e).strip() or "Ошибка экспорта архива")


class ProfilesArchiveImportThread(QThread):
    """Импорт ZIP: объединение с текущими профилями, при конфликте ID — новый profile_id."""

    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(int, int)  # added_count, remapped_count
    failed = pyqtSignal(str)

    def __init__(self, zip_path: str, existing: list[BrowserProfile]) -> None:
        super().__init__()
        self._zip_path = zip_path
        self._existing = existing

    def run(self) -> None:
        from profiles_bundle import import_profiles_zip

        try:
            _merged, added, remapped = import_profiles_zip(
                Path(self._zip_path),
                list(self._existing),
                progress=lambda s: self.progress.emit(s),
            )
            self.finished_ok.emit(added, remapped)
        except Exception as e:
            self.failed.emit(str(e).strip() or "Ошибка импорта архива")


def _tag_chip_object_name(tag: str) -> str:
    """Имя objectName для QSS: ошибка — красный, успех — зелёный, иначе дефолт."""
    low = tag.casefold()
    if "ошибка" in low:
        return "error"
    if "успех" in low or low.startswith("успеш"):
        return "success"
    return "tagChip"


class _TagChip(QFrame):
    """Отображение одного тега; в редакторе — удаление и правка по двойному клику."""

    removed = pyqtSignal(str)
    edited = pyqtSignal(str, str)

    def __init__(
        self,
        tag: str,
        parent: QWidget | None = None,
        *,
        removable: bool = True,
        editable: bool = False,
        fixed_width: int | None = None,
    ) -> None:
        super().__init__(parent)
        self._tag = tag
        self._fixed_width = fixed_width
        self._editable = editable and removable
        self._editing = False
        self._close_btn: QPushButton | None = None
        self._edit: QLineEdit | None = None
        self.setObjectName(_tag_chip_object_name(tag))
        self._lay = QHBoxLayout(self)
        self._lay.setSpacing(0)
        self._lbl = QLabel(tag)
        self._lbl.setWordWrap(True)
        tip = tag
        if self._editable:
            tip = f"{tag}\n\nДвойной клик — изменить"
        self._lbl.setToolTip(tip)
        if self._editable:
            # Клики идут на QFrame — иначе двойной клик по тексту не открывает редактирование.
            self._lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._lbl.installEventFilter(self)
        self._lbl.ensurePolished()
        chip_font = self._lbl.font()
        if fixed_width is not None:
            text_w = _tag_chip_text_width(fixed_width, removable=removable)
            lbl_w = _tag_chip_label_width(fixed_width, removable=removable)
            self._lbl.setMinimumWidth(lbl_w)
            self._lbl.setMinimumHeight(_tag_chip_text_height(tag, text_w, chip_font))
            self._lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
            self._lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            need_h = _tag_chip_content_height(tag, fixed_width, chip_font, removable=removable)
            self.setMinimumHeight(need_h)
        if removable:
            self._lay.setContentsMargins(
                _TAG_CHIP_MARGIN_H, _TAG_CHIP_MARGIN_V, 6, _TAG_CHIP_MARGIN_V
            )
            self._close_btn = QPushButton("×")
            self._close_btn.setObjectName("tagChipClose")
            self._close_btn.setFixedSize(22, 22)
            self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._close_btn.setToolTip("Удалить тег")
            self._close_btn.clicked.connect(lambda: self.removed.emit(self._tag))
            self._lay.addWidget(self._lbl, 1)
            self._lay.addWidget(self._close_btn, 0, Qt.AlignmentFlag.AlignTop)
        else:
            self._lay.setContentsMargins(
                _TAG_CHIP_MARGIN_H,
                _TAG_CHIP_MARGIN_V,
                _TAG_CHIP_MARGIN_H,
                _TAG_CHIP_MARGIN_V,
            )
            self._lay.addWidget(self._lbl, 1)
        if fixed_width is None:
            self.setMinimumHeight(_tag_chip_min_height(self._lbl.font()))
        if fixed_width is not None:
            self.setFixedWidth(fixed_width)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)

    def mouseDoubleClickEvent(self, a0: QMouseEvent | None) -> None:
        if self._editable and a0 is not None and a0.button() == Qt.MouseButton.LeftButton:
            self._begin_edit()
            a0.accept()
            return
        super().mouseDoubleClickEvent(a0)

    def _edit_inner_width(self) -> int | None:
        if self._fixed_width is None:
            return None
        return _tag_chip_label_width(self._fixed_width, removable=True)

    def _begin_edit(self) -> None:
        if self._editing or not self._editable:
            return
        self._editing = True
        self._lbl.hide()
        if self._close_btn is not None:
            self._close_btn.hide()
        self._edit = QLineEdit(self._tag, self)
        self._edit.setFrame(False)
        inner_w = self._edit_inner_width()
        if inner_w is not None:
            self._edit.setFixedWidth(inner_w)
        self._edit.returnPressed.connect(self._commit_edit)
        self._edit.editingFinished.connect(self._commit_edit)
        self._edit.installEventFilter(self)
        self._lay.insertWidget(0, self._edit, 1)
        self._edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._edit.selectAll()

    def _cancel_edit(self) -> None:
        if not self._editing:
            return
        self._editing = False
        if self._edit is not None:
            self._edit.removeEventFilter(self)
            self._lay.removeWidget(self._edit)
            self._edit.deleteLater()
            self._edit = None
        self._lbl.show()
        if self._close_btn is not None:
            self._close_btn.show()

    def _commit_edit(self) -> None:
        if not self._editing or self._edit is None:
            return
        new = (self._edit.text() or "").strip()
        old = self._tag
        self._cancel_edit()
        if not new or new == old:
            return
        self.edited.emit(old, new)

    def eventFilter(self, watched: QObject, event) -> bool:  # type: ignore[override]
        if watched is self._lbl and event.type() == QEvent.Type.MouseButtonDblClick:
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                self._begin_edit()
                return True
        if watched is self._edit and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._cancel_edit()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_edit()
                return True
        return super().eventFilter(watched, event)


_PROFILE_TAGS_PER_ROW = 2
_PROFILE_TAG_CHIP_MIN_WIDTH = 88
_PROFILE_TAG_CHIP_MAX_WIDTH = 360
_PROFILE_TAG_ROW_SPACING = 6
_PROFILE_TAGS_SCROLL_MIN_H = 100
_PROFILE_TAGS_SCROLL_MAX_H = 300
_TAG_CHIP_MARGIN_H = 6
_TAG_CHIP_MARGIN_V = 4
_TAG_CHIP_LBL_PAD_H = 6
_TAG_CHIP_LBL_PAD_V = 4
_TAG_CHIP_BORDER_W = 2
_TAG_CHIP_CLOSE_BTN_W = 22
_TAG_CHIP_CLOSE_GAP = 6
_TAG_CHIP_HEIGHT_SLACK = 2


def _tag_chip_frame_horizontal(*, removable: bool) -> int:
    """Рамка QFrame + отступы layout (без QLabel и без кнопки ×)."""
    return _TAG_CHIP_MARGIN_H * 2 + _TAG_CHIP_BORDER_W


def _tag_chip_extra_horizontal(*, removable: bool) -> int:
    """Всё горизонтальное пространство чипа, кроме текста."""
    extra = _tag_chip_frame_horizontal(removable=removable) + _TAG_CHIP_LBL_PAD_H * 2
    if removable:
        extra += _TAG_CHIP_CLOSE_BTN_W + _TAG_CHIP_CLOSE_GAP
    return extra


def _tag_chip_label_width(chip_w: int, *, removable: bool) -> int:
    """Ширина виджета QLabel внутри чипа."""
    btn = (_TAG_CHIP_CLOSE_BTN_W + _TAG_CHIP_CLOSE_GAP) if removable else 0
    return max(1, chip_w - _tag_chip_frame_horizontal(removable=removable) - btn)


def _tag_chip_text_width(chip_w: int, *, removable: bool) -> int:
    """Ширина области текста (с учётом padding QLabel в QSS)."""
    return max(1, _tag_chip_label_width(chip_w, removable=removable) - _TAG_CHIP_LBL_PAD_H * 2)


def _tag_chip_natural_width(tag: str, font, *, removable: bool = False) -> int:
    fm = QFontMetrics(font)
    text = fm.horizontalAdvance(tag)
    natural = text + _tag_chip_extra_horizontal(removable=removable)
    return max(_PROFILE_TAG_CHIP_MIN_WIDTH, min(natural, _PROFILE_TAG_CHIP_MAX_WIDTH))


def _tag_chip_column_widths(tags: list[str], font, *, removable: bool = False) -> list[int]:
    """Ширина каждой колонки сетки (2 колонки) по самому длинному тегу в колонке."""
    cols = _PROFILE_TAGS_PER_ROW
    widths = [_PROFILE_TAG_CHIP_MIN_WIDTH] * cols
    for idx, tag in enumerate(tags):
        col = idx % cols
        widths[col] = max(widths[col], _tag_chip_natural_width(tag, font, removable=removable))
    return widths


def _tag_chip_text_height(tag: str, text_w: int, font: QFont) -> int:
    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WordWrap)
    doc.setDefaultTextOption(opt)
    doc.setPlainText(tag)
    doc.setTextWidth(float(max(1, text_w)))
    return int(doc.size().height())


def _tag_chip_vertical_extras() -> int:
    return 2 * (_TAG_CHIP_LBL_PAD_V + _TAG_CHIP_MARGIN_V) + _TAG_CHIP_HEIGHT_SLACK


def _tag_chip_min_height(font) -> int:
    return QFontMetrics(font).height() + _tag_chip_vertical_extras()


def _tag_chip_content_height(
    tag: str, chip_w: int, font: QFont, *, removable: bool
) -> int:
    text_w = _tag_chip_text_width(chip_w, removable=removable)
    return _tag_chip_text_height(tag, text_w, font) + _tag_chip_vertical_extras()


def _make_profile_tags_widget(
    tags: list[str],
    parent: QWidget | None = None,
    *,
    removable: bool = False,
    on_removed: Callable[[str], None] | None = None,
    on_edited: Callable[[str, str], None] | None = None,
) -> QWidget:
    """Чипы тегов: 2 колонки, одинаковая ширина, перенос по рядам."""
    w = QWidget(parent)
    cols = _PROFILE_TAGS_PER_ROW
    grid = QGridLayout(w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(_PROFILE_TAG_ROW_SPACING)
    grid.setVerticalSpacing(4)
    col_widths = _tag_chip_column_widths(tags, w.font(), removable=removable)
    for c in range(cols):
        grid.setColumnMinimumWidth(c, col_widths[c])
        grid.setColumnStretch(c, 1)
    block_w = sum(col_widths) + (cols - 1) * _PROFILE_TAG_ROW_SPACING
    w.setFixedWidth(block_w)
    row_heights: dict[int, int] = {}
    pending: list[tuple[int, int, _TagChip]] = []
    for idx, tag in enumerate(tags):
        row_i = idx // cols
        col_i = idx % cols
        chip_w = col_widths[col_i]
        chip = _TagChip(
            tag,
            w,
            removable=removable,
            editable=removable and on_edited is not None,
            fixed_width=chip_w,
        )
        if removable and on_removed is not None:
            chip.removed.connect(on_removed)
        if removable and on_edited is not None:
            chip.edited.connect(on_edited)
        row_heights[row_i] = max(row_heights.get(row_i, 0), chip.minimumHeight())
        pending.append((row_i, col_i, chip))
    for row_i, col_i, chip in pending:
        chip.setMinimumHeight(row_heights[row_i])
        grid.addWidget(chip, row_i, col_i, Qt.AlignmentFlag.AlignTop)
    for row_i, mh in row_heights.items():
        grid.setRowMinimumHeight(row_i, mh)
    grid_h = sum(row_heights.values()) + max(0, len(row_heights) - 1) * grid.verticalSpacing()
    w.setMinimumHeight(grid_h)
    w.adjustSize()
    return w


def _profile_tags_grid_height(
    tags: list[str],
    font,
    col_widths: list[int],
    *,
    removable: bool = False,
) -> int:
    if not tags:
        return _PROFILE_TAGS_SCROLL_MIN_H
    cols = _PROFILE_TAGS_PER_ROW
    total = 4
    for i in range(0, len(tags), cols):
        row_h = 0
        for j, t in enumerate(tags[i : i + cols]):
            row_h = max(row_h, _tag_chip_content_height(t, col_widths[j], font, removable=removable))
        total += row_h
        if i + cols < len(tags):
            total += 4
    return max(_PROFILE_TAGS_SCROLL_MIN_H, total)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Antidetect (Playwright Profiles) — UI")
        self.setMinimumSize(1060, 680)

        self._editable_tags: list[str] = []
        self._checked_profile_ids: set[str] = set()
        self._profile_id_to_checkbox: dict[str, QCheckBox] = {}
        self._profile_id_to_row: dict[str, QWidget] = {}
        self._profile_id_to_title_label: dict[str, QLabel] = {}
        self._profile_id_to_id_label: dict[str, QLabel] = {}
        self._profile_row_filter_widgets: set[QWidget] = set()
        self._syncing_selection_check: bool = False
        # LMB «краска» по чекбоксам (Ctrl — добавить к уже отмеченным).
        self._lmb_select_active: bool = False
        self._lmb_select_additive: bool = False
        self._lmb_select_base: set[str] = set()
        self._lmb_select_visited: set[str] = set()
        self._lmb_select_last_row: int | None = None
        self._checkbox_row_pending: int | None = None
        self._checkbox_press_global: QPoint | None = None
        self._checkbox_press_modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
        # Клик по строке (не чекбокс): открыть настройки без отметки.
        self._title_row_pending: int | None = None
        self._title_press_global: QPoint | None = None
        self._title_press_modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier

        root = QWidget()
        root.setObjectName("zaliverRoot")
        self.setCentralWidget(root)

        self._profiles: list[BrowserProfile] = load_profiles()
        self._active_profile_id: Optional[str] = self._profiles[0].profile_id if self._profiles else None
        self._runners: dict[str, RunnerThread] = {}
        self._run_buttons: dict[str, QPushButton] = {}
        self._profile_id_to_item: dict[str, QListWidgetItem] = {}
        self._proxy_health_thread: ProxyHealthCheckThread | None = None
        self._import_health_thread: BatchImportProxyHealthThread | None = None
        self._import_health_dialog: ProxyBatchCheckProgressDialog | None = None
        self._import_build_thread: ImportProfilesBuildThread | None = None
        self._archive_export_thread: ProfilesArchiveExportThread | None = None
        self._archive_import_thread: ProfilesArchiveImportThread | None = None
        self._proxy_single_check_dialog: QProgressDialog | None = None
        self._proxies_table_refreshing = False
        self._pending_open_profile_id: str | None = None
        self._metadata_sync_timer = QTimer(self)
        self._metadata_sync_timer.setSingleShot(True)
        self._metadata_sync_timer.setInterval(300)
        self._metadata_sync_timer.timeout.connect(self._flush_metadata_sync_from_disk)

        layout = QHBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setHandleWidth(6)
        layout.addWidget(self._main_splitter)

        # left nav (ширину меняют перетаскиванием разделителя)
        self._side_nav = QListWidget()
        self._side_nav.setObjectName("sideNav")
        self._side_nav.addItem("Профили")
        self._side_nav.addItem("Прокси")
        self._side_nav.setMinimumWidth(56)
        self._side_nav.setMaximumWidth(360)
        self._side_nav.setCurrentRow(0)

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
            sync_profile_metadata=self._api_bridge.sync_profile_metadata.emit,
        )

        self._main_splitter.addWidget(self._side_nav)
        self._main_splitter.addWidget(self.pages)
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setCollapsible(0, True)
        self._main_splitter.setCollapsible(1, False)
        self._main_splitter.setSizes([180, 880])

        self._side_nav.currentRowChanged.connect(self._on_nav_changed)

        self._apply_theme()
        self._setup_proxy_refresh_icon()
        self._refresh_profiles_list()
        self._load_active_profile_into_form()

    def _proxy_health_dot_ui(self, p: BrowserProfile) -> tuple[str, str, str]:
        """
        Returns: (text, stylesheet, tooltip)
        text is usually "●" or "".
        """
        srv = (p.proxy_server or "").strip()
        if not srv:
            return "", "", "Прокси не задан"
        if p.proxy_health_ok is True:
            color = "#6c6"
            tip = (p.proxy_health_message or "OK").strip() or "OK"
        elif p.proxy_health_ok is False:
            color = "#c66"
            tip = (p.proxy_health_message or "Ошибка").strip() or "Ошибка"
        else:
            color = "#888"
            tip = "Прокси не проверен"
        if p.proxy_health_checked_at:
            tip = f"{tip}\n{p.proxy_health_checked_at}"
        tip = f"{srv}\n{tip}".strip()
        return "●", f"color: {color};", tip

    def _expand_field(self, w: QWidget, *, min_w: int = 420) -> None:
        sp = w.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
        w.setSizePolicy(sp)
        w.setMinimumWidth(min_w)

    def _custom_data_key_count(self, data: dict | None = None) -> int:
        if data is not None:
            return len(data or {})
        raw = (self.ed_custom_data.toPlainText() if hasattr(self, "ed_custom_data") else "") or ""
        if not raw.strip():
            return 0
        try:
            return len(custom_data_from_json_text(raw))
        except (json.JSONDecodeError, ValueError):
            p = self._active_profile()
            return len(p.custom_data) if p else 0

    def _update_custom_data_toggle_label(self, *, expanded: bool | None = None) -> None:
        if not hasattr(self, "btn_custom_data_toggle"):
            return
        n = self._custom_data_key_count()
        open_ = self._custom_data_expanded if expanded is None else expanded
        arrow = "▾" if open_ else "▸"
        suffix = f" ({n})" if n else " (пусто)"
        self.btn_custom_data_toggle.setText(f"{arrow} Доп. данные{suffix}")

    def _collapse_custom_data_panel(self) -> None:
        self._custom_data_expanded = False
        self._custom_data_panel.hide()
        self._update_custom_data_toggle_label(expanded=False)

    def _toggle_custom_data_panel(self) -> None:
        self._custom_data_expanded = not self._custom_data_expanded
        self._custom_data_panel.setVisible(self._custom_data_expanded)
        self._update_custom_data_toggle_label(expanded=self._custom_data_expanded)

    def _rebuild_tag_chips(self) -> None:
        lay = self._tags_chips_layout
        while lay.count():
            it = lay.takeAt(0)
            if w := it.widget():
                w.deleteLater()
        if self._editable_tags:
            lay.addWidget(
                _make_profile_tags_widget(
                    self._editable_tags,
                    self._tags_chips_host,
                    removable=True,
                    on_removed=self._on_tag_chip_removed,
                    on_edited=self._on_tag_chip_edited,
                ),
                0,
            )
        font = self._tags_chips_host.font()
        col_widths = _tag_chip_column_widths(self._editable_tags, font, removable=True)
        h = _profile_tags_grid_height(self._editable_tags, font, col_widths, removable=True)
        self._tags_scroll.setFixedHeight(
            max(_PROFILE_TAGS_SCROLL_MIN_H, min(h, _PROFILE_TAGS_SCROLL_MAX_H))
        )

    def _on_tag_chip_removed(self, tag: str) -> None:
        try:
            self._editable_tags.remove(tag)
        except ValueError:
            pass
        self._rebuild_tag_chips()

    def _on_tag_chip_edited(self, old: str, new: str) -> None:
        new = new.strip()
        if not new or new == old:
            return
        try:
            idx = self._editable_tags.index(old)
        except ValueError:
            return
        if any(t == new for i, t in enumerate(self._editable_tags) if i != idx):
            QMessageBox.warning(self, "Теги", f"Тег «{new}» уже есть в списке.")
            self._rebuild_tag_chips()
            return
        self._editable_tags[idx] = new
        self._rebuild_tag_chips()

    def _on_commit_new_tags(self) -> None:
        raw = (self.ed_tag_add.text() or "").strip()
        if not raw:
            return
        for t in tags_from_delimited_text(raw):
            if t not in self._editable_tags:
                self._editable_tags.append(t)
        self.ed_tag_add.clear()
        self._rebuild_tag_chips()

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app:
            app.setStyleSheet(ZALIVER_DARK_QSS)

    def _build_profiles_page(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(10)

        title = QLabel("Профили")
        title.setObjectName("title")
        l.addWidget(title)

        self._profiles_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._profiles_splitter.setHandleWidth(6)
        self._profiles_splitter.setChildrenCollapsible(True)
        l.addWidget(self._profiles_splitter, 1)

        # list box
        list_box = QGroupBox("Список профилей")
        list_layout = QVBoxLayout(list_box)
        list_layout.setSpacing(10)

        self.ed_profiles_search = QLineEdit()
        self.ed_profiles_search.setPlaceholderText("Поиск: имя / описание / теги / ID / прокси…")
        self._expand_field(self.ed_profiles_search, min_w=240)
        self.ed_profiles_search.textChanged.connect(lambda _t: self._refresh_profiles_list())

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(self.ed_profiles_search, 1)
        list_layout.addLayout(search_row)

        list_sel_row = QHBoxLayout()
        list_sel_row.addStretch(1)
        self.lbl_checked_profiles_count = QLabel("Выделено: 0")
        self.lbl_checked_profiles_count.setObjectName("hint")
        self.lbl_checked_profiles_count.setToolTip("Число профилей с отмеченным квадратиком в списке")
        list_sel_row.addWidget(self.lbl_checked_profiles_count)
        self.btn_clear_profile_selection = QPushButton("Снять выделение")
        self.btn_clear_profile_selection.setObjectName("secondary")
        self.btn_clear_profile_selection.setToolTip("Убрать все отметки и выделение в списке профилей")
        self.btn_clear_profile_selection.clicked.connect(self._clear_profiles_list_selection)
        list_sel_row.addWidget(self.btn_clear_profile_selection)
        list_layout.addLayout(list_sel_row)

        self.profiles_list = QListWidget()
        self.profiles_list.setObjectName("profilesList")
        # Отметка — только чекбокс (клики по строке перехватывает eventFilter).
        self.profiles_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.profiles_list.setMouseTracking(True)
        self.profiles_list.viewport().setMouseTracking(True)
        self.profiles_list.currentItemChanged.connect(self._on_profile_current_changed)
        self.profiles_list.itemSelectionChanged.connect(self._on_profiles_list_item_selection_changed)
        self.profiles_list.installEventFilter(self)
        # После grabMouse() события идут во viewport, не в QListWidget.
        self.profiles_list.viewport().installEventFilter(self)
        list_layout.addWidget(self.profiles_list, 1)

        launch_box = QGroupBox("Запуск")
        launch_form = QFormLayout(launch_box)
        launch_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        launch_form.setHorizontalSpacing(12)
        launch_form.setVerticalSpacing(10)
        launch_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.ed_url = QLineEdit("https://studio.youtube.com")
        self._expand_field(self.ed_url)
        launch_form.addRow("Стартовый URL", self.ed_url)

        launch_btns = QHBoxLayout()
        self.btn_launch_selected = QPushButton("Запустить выбранные")
        self.btn_launch_selected.setObjectName("secondary")
        self.btn_launch_selected.clicked.connect(self._launch_selected_from_profiles_list)
        launch_btns.addStretch(1)
        launch_btns.addWidget(self.btn_launch_selected)
        launch_form.addRow("", launch_btns)

        list_layout.addWidget(launch_box)

        btn_row = QHBoxLayout()
        self.btn_new = QPushButton("Новый")
        self.btn_new.setObjectName("secondary")
        self.btn_import_proxies = QPushButton("Прокси из файла…")
        self.btn_import_proxies.setObjectName("secondary")
        self.btn_import_proxies.setToolTip("Текстовый файл: по одной строке host:port:user:pass")
        self.btn_export_archive = QPushButton("Экспорт")
        self.btn_export_archive.setObjectName("secondary")
        self.btn_export_archive.setToolTip(
            "Сохранить ZIP: полный user-data или только cookies (с выбором сайтов). "
            "Если отмечены профили — только они; иначе все профили."
        )
        self.btn_import_archive = QPushButton("Импорт")
        self.btn_import_archive.setObjectName("secondary")
        self.btn_import_archive.setToolTip(
            "ZIP из «Экспорт» (полный или cookies): профили добавятся к текущим; "
            "при совпадении ID будет назначен новый."
        )
        self.btn_clear = QPushButton("Очистить")
        self.btn_clear.setObjectName("danger")
        self.btn_clear.setToolTip(
            "Действия для выделенных профилей; если ничего не выделено — текущий открытый в форме: "
            "очистка данных браузера и/или смена отпечатка; теги и custom_data сбрасываются. "
            "Имя и прокси сохраняются."
        )
        self.btn_delete = QPushButton("Удалить")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.setToolTip("Удалить все выделенные профили; если ничего не выделено — текущий открытый в форме")
        btn_row.addWidget(self.btn_new)
        btn_row.addWidget(self.btn_import_proxies)
        btn_row.addWidget(self.btn_export_archive)
        btn_row.addWidget(self.btn_import_archive)
        btn_row.addWidget(self.btn_clear)
        btn_row.addWidget(self.btn_delete)
        list_layout.addLayout(btn_row)

        self.btn_new.clicked.connect(self._create_profile)
        self.btn_import_proxies.clicked.connect(self._import_profiles_from_proxy_file)
        self.btn_export_archive.clicked.connect(self._export_profiles_archive)
        self.btn_import_archive.clicked.connect(self._import_profiles_archive)
        self.btn_clear.clicked.connect(self._clear_profiles)
        self.btn_delete.clicked.connect(self._delete_profile)

        list_box.setMinimumWidth(260)
        self._profiles_splitter.addWidget(list_box)

        editor = QGroupBox("Настройки профиля")
        editor_inner = QVBoxLayout(editor)
        editor_inner.setContentsMargins(0, 0, 0, 0)
        editor_inner.setSpacing(10)
        form_panel = QWidget()
        form = QFormLayout(form_panel)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.ed_name = QLineEdit()
        self.ed_proxy_server = QLineEdit()
        self.ed_proxy_server.setPlaceholderText("1.2.3.4:8080 или http://host:port / socks5://…")
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

        self._tags_chips_host = QWidget()
        self._tags_chips_layout = QVBoxLayout(self._tags_chips_host)
        self._tags_chips_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_chips_layout.setSpacing(0)

        self._tags_scroll = QScrollArea()
        self._tags_scroll.setWidgetResizable(True)
        self._tags_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tags_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._tags_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._tags_scroll.setFixedHeight(_PROFILE_TAGS_SCROLL_MIN_H)
        self._tags_scroll.setWidget(self._tags_chips_host)

        self.ed_tag_add = QLineEdit()
        self.ed_tag_add.setPlaceholderText("Новый тег; несколько через запятую")

        self.btn_tag_add = QPushButton("Добавить")
        self.btn_tag_add.setObjectName("secondary")
        self.btn_tag_add.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.btn_tag_add.clicked.connect(self._on_commit_new_tags)
        self.ed_tag_add.returnPressed.connect(self._on_commit_new_tags)
        tag_add_row = QHBoxLayout()
        tag_add_row.setSpacing(8)
        tag_add_row.addWidget(self.ed_tag_add, 1)
        tag_add_row.addWidget(self.btn_tag_add)

        self._tags_input_block = QWidget()
        _til = QVBoxLayout(self._tags_input_block)
        _til.setContentsMargins(0, 0, 0, 0)
        _til.setSpacing(4)
        _til.addWidget(self._tags_scroll)
        _til.addLayout(tag_add_row)

        self.ed_description = QPlainTextEdit()
        self.ed_description.setPlaceholderText("Заметки к профилю…")
        self.ed_description.setFixedHeight(48)
        self.ed_description.setTabChangesFocus(True)
        self.ed_custom_data = QPlainTextEdit()
        self.ed_custom_data.setPlaceholderText('{"ключ": "значение"} — JSON-объект')
        self.ed_custom_data.setFixedHeight(72)
        self.ed_custom_data.setTabChangesFocus(True)
        self._expand_field(self._tags_input_block)
        self._expand_field(self.ed_description)

        self._custom_data_panel = QWidget()
        _cdl = QVBoxLayout(self._custom_data_panel)
        _cdl.setContentsMargins(0, 0, 0, 0)
        _cdl.setSpacing(0)
        _cdl.addWidget(self.ed_custom_data)
        self._expand_field(self.ed_custom_data)
        self._custom_data_panel.hide()

        self.btn_custom_data_toggle = QPushButton("▸ Доп. данные")
        self.btn_custom_data_toggle.setFlat(True)
        self.btn_custom_data_toggle.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_custom_data_toggle.clicked.connect(self._toggle_custom_data_panel)
        self._custom_data_expanded = False

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
        form.addRow("Отпечаток", self.ed_ua)
        form.addRow("Страна прокси", self.ed_tz)
        form.addRow(self._hr())
        # WebGL parameters are stored in profiles, but intentionally hidden from UI.

        lbl_tags = QLabel("Теги")
        lbl_tags.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl_desc = QLabel("Описание")
        lbl_desc.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        lbl_desc.setContentsMargins(0, 3, 0, 0)
        _fm = lbl_tags.fontMetrics()
        _lab_w = max(_fm.horizontalAdvance("Описание"), _fm.horizontalAdvance("Теги")) + 6
        lbl_tags.setFixedWidth(_lab_w)
        lbl_desc.setFixedWidth(_lab_w)

        tags_row = QHBoxLayout()
        tags_row.setSpacing(8)
        tags_row.addWidget(lbl_tags, 0)
        tags_row.addWidget(self._tags_input_block, 1)

        desc_row = QHBoxLayout()
        desc_row.setSpacing(8)
        desc_row.addWidget(lbl_desc, 0, Qt.AlignmentFlag.AlignTop)
        desc_row.addWidget(self.ed_description, 1)

        meta_left = QWidget()
        meta_l = QVBoxLayout(meta_left)
        meta_l.setContentsMargins(0, 0, 0, 0)
        meta_l.setSpacing(6)
        meta_l.addLayout(tags_row)
        meta_l.addLayout(desc_row)
        meta_l.addWidget(self.btn_custom_data_toggle, 0)
        meta_l.addWidget(self._custom_data_panel, 0)

        self.btn_save = QPushButton("Сохранить профиль")
        self.btn_save.setObjectName("secondary")
        self.btn_save.clicked.connect(self._save_active_profile)
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_row.addWidget(self.btn_save)

        editor_inner.addWidget(form_panel, 0)
        editor_inner.addWidget(meta_left)
        editor_inner.addLayout(save_row)

        l2 = QVBoxLayout()
        l2.setContentsMargins(0, 0, 0, 0)
        l2.setSpacing(0)
        l2.addWidget(editor, 0)
        profile_settings_col = QWidget()
        profile_settings_col.setLayout(l2)
        # Строка тегов (подпись + поле + «Добавить») и поля формы — не уже их minimumWidth.
        _form_label_w = max(
            _fm.horizontalAdvance(t)
            for t in ("Прокси (пароль)", "Прокси (сервер)", "Страна прокси", "Отпечаток")
        )
        _settings_min_w = max(
            _lab_w + tags_row.spacing() + self._tags_input_block.minimumWidth() + 24,
            _form_label_w + form.horizontalSpacing() + 420 + 24,
        )
        profile_settings_col.setMinimumWidth(_settings_min_w)
        editor.setMinimumWidth(_settings_min_w)
        self._profiles_splitter.addWidget(profile_settings_col)
        self._profiles_splitter.setStretchFactor(0, 2)
        self._profiles_splitter.setStretchFactor(1, 1)
        self._profiles_splitter.setCollapsible(0, False)
        self._profiles_splitter.setCollapsible(1, True)

        logs_box = QGroupBox("Логи")
        logs_l = QVBoxLayout(logs_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        logs_l.addWidget(self.log, 1)
        logs_box.setMaximumHeight(220)
        l.addWidget(logs_box, 0)

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

    def _release_profiles_list_mouse_grab(self) -> None:
        """Снять grab с viewport списка профилей (иначе возможен краш Qt при смене вкладки)."""
        self._clear_profile_title_click_pending()
        self._clear_checkbox_click_pending()
        if self._lmb_select_active:
            self._lmb_select_active = False
            self._lmb_select_additive = False
            self._lmb_select_base.clear()
            self._lmb_select_visited.clear()
            self._lmb_select_last_row = None
        try:
            self.profiles_list.viewport().releaseMouse()
        except Exception:
            pass

    def _focus_profile_in_list(self, profile_id: str) -> bool:
        """Выделить профиль в списке и загрузить его в редактор (без смены вкладки)."""
        if not any(p.profile_id == profile_id for p in self._profiles):
            return False
        if hasattr(self, "ed_profiles_search") and (self.ed_profiles_search.text() or "").strip():
            self.ed_profiles_search.blockSignals(True)
            try:
                self.ed_profiles_search.clear()
            finally:
                self.ed_profiles_search.blockSignals(False)
            self._refresh_profiles_list()
        elif profile_id not in self._profile_id_to_item:
            self._refresh_profiles_list()
        it = self._profile_id_to_item.get(profile_id)
        if it is None:
            return False
        self._active_profile_id = profile_id
        self._apply_active_profile_row_visuals()
        self._set_current_profile_list_item(it)
        self.profiles_list.scrollToItem(it, QAbstractItemView.ScrollHint.EnsureVisible)
        self._load_active_profile_into_form()
        return True

    def _copy_profile_id_to_clipboard(self, profile_id: str) -> None:
        QApplication.clipboard().setText(profile_id)

    def _open_profile_from_proxies_tab(self, profile_id: str) -> None:
        """Отложенный переход с главного окна (не с виджета в ячейке таблицы)."""
        self._pending_open_profile_id = profile_id
        QTimer.singleShot(0, self._open_pending_profile_from_proxies_tab)

    def _open_pending_profile_from_proxies_tab(self) -> None:
        profile_id = self._pending_open_profile_id
        self._pending_open_profile_id = None
        if not profile_id:
            return
        if not any(p.profile_id == profile_id for p in self._profiles):
            QMessageBox.information(self, "Профили", f"Профиль «{profile_id}» не найден.")
            return
        self._release_profiles_list_mouse_grab()
        self._side_nav.setCurrentRow(0)
        if not self._focus_profile_in_list(profile_id):
            QMessageBox.information(self, "Профили", f"Профиль «{profile_id}» не найден в списке.")

    def _proxies_stat_card(
        self,
        label: str,
        *,
        frame_name: str,
        value_name: str,
    ) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName(frame_name)
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(14, 12, 14, 12)
        card_l.setSpacing(4)
        value_lbl = QLabel("0")
        value_lbl.setObjectName(value_name)
        value_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl = QLabel(label)
        title_lbl.setObjectName("proxiesStatLabel")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_l.addWidget(value_lbl)
        card_l.addWidget(title_lbl)
        return card, value_lbl

    def _build_proxies_page(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(14)

        header = QFrame()
        header.setObjectName("proxiesHeader")
        header_l = QVBoxLayout(header)
        header_l.setContentsMargins(18, 16, 18, 16)
        header_l.setSpacing(6)
        title = QLabel("Прокси")
        title.setObjectName("title")
        hint = QLabel(
            ""
        )
        hint.setObjectName("proxiesHeaderHint")
        hint.setWordWrap(True)
        header_l.addWidget(title)
        header_l.addWidget(hint)
        l.addWidget(header)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        card_total, self.lbl_proxies_stat_total = self._proxies_stat_card(
            "Всего",
            frame_name="proxiesStatCard",
            value_name="proxiesStatValue",
        )
        card_ok, self.lbl_proxies_stat_ok = self._proxies_stat_card(
            "Рабочие",
            frame_name="proxiesStatCardOk",
            value_name="proxiesStatValueOk",
        )
        card_fail, self.lbl_proxies_stat_fail = self._proxies_stat_card(
            "Нерабочие",
            frame_name="proxiesStatCardFail",
            value_name="proxiesStatValueFail",
        )
        card_unknown, self.lbl_proxies_stat_unknown = self._proxies_stat_card(
            "Не проверены",
            frame_name="proxiesStatCardUnknown",
            value_name="proxiesStatValueUnknown",
        )
        for card in (card_total, card_ok, card_fail, card_unknown):
            stats_row.addWidget(card, 1)
        l.addLayout(stats_row)

        self.ed_proxies_search = QLineEdit()
        self.ed_proxies_search.setPlaceholderText(
            "Поиск: адрес сервера, логин или ID профиля"
        )
        self.ed_proxies_search.textChanged.connect(lambda _t: self._refresh_proxies_page_table())
        l.addWidget(self.ed_proxies_search, 0)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.btn_check_all_proxies = QPushButton("Проверить все прокси")
        self.btn_check_all_proxies.setToolTip(
            f"Параллельно проверить каждый уникальный прокси (до {PROXY_HEALTH_BATCH_MAX_WORKERS} одновременно)"
        )
        self.btn_check_all_proxies.clicked.connect(self._on_check_all_proxies_click)
        self.btn_refresh_proxies_table = QPushButton("Обновить список")
        self.btn_refresh_proxies_table.setObjectName("secondary")
        self.btn_refresh_proxies_table.setToolTip("Перечитать таблицу из сохранённых профилей")
        self.btn_refresh_proxies_table.clicked.connect(self._refresh_proxies_page_table)
        toolbar.addWidget(self.btn_check_all_proxies)
        toolbar.addWidget(self.btn_refresh_proxies_table)
        toolbar.addStretch(1)
        l.addLayout(toolbar)

        table_frame = QFrame()
        table_frame.setObjectName("proxiesTableFrame")
        table_l = QVBoxLayout(table_frame)
        table_l.setContentsMargins(10, 10, 10, 10)
        self.table_proxies = QTableWidget(0, 8)
        self.table_proxies.setObjectName("proxiesTable")
        self.table_proxies.setHorizontalHeaderLabels(
            ["Сервер", "Логин", "Пароль", "ID профилей", "Статус", "Проверено", "Профилей", "↻"],
        )
        self.table_proxies.verticalHeader().setVisible(False)
        self.table_proxies.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_proxies.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed,
        )
        self.table_proxies.setAlternatingRowColors(True)
        self.table_proxies.setShowGrid(False)
        # Сортировка Qt + setCellWidget() на Windows часто даёт 0xC0000409 — сортируем в Python.
        self.table_proxies.setSortingEnabled(False)
        self.table_proxies.cellChanged.connect(self._on_proxies_table_cell_changed)
        self._proxy_password_delegate = ProxyPasswordDelegate(self.table_proxies)
        self.table_proxies.setItemDelegateForColumn(_PROXY_COL_PASSWORD, self._proxy_password_delegate)
        hh = self.table_proxies.horizontalHeader()
        hh.setSectionResizeMode(_PROXY_COL_SERVER, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(_PROXY_COL_LOGIN, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_PROXY_COL_PASSWORD, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_PROXY_COL_IDS, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(_PROXY_COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_PROXY_COL_CHECKED, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_PROXY_COL_COUNT, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_PROXY_COL_REFRESH, QHeaderView.ResizeMode.Fixed)
        self.table_proxies.setColumnWidth(_PROXY_COL_REFRESH, 52)
        table_l.addWidget(self.table_proxies)
        l.addWidget(table_frame, 1)
        return w

    def _group_profiles_by_proxy(self) -> dict[tuple[str, str | None, str | None], list[BrowserProfile]]:
        """Одна строка таблицы = уникальный прокси (URL + логин + пароль), все профили в группе."""
        groups: dict[tuple[str, str | None, str | None], list[BrowserProfile]] = {}
        for p in self._profiles:
            key = canonical_proxy_key(p.proxy_server, p.proxy_username, p.proxy_password)
            if not key:
                continue
            bucket = groups.setdefault(key, [])
            if any(m.profile_id == p.profile_id for m in bucket):
                continue
            bucket.append(p)
        return groups

    def _newest_health_in_group(self, members: list[BrowserProfile]) -> tuple[bool | None, str | None, str | None]:
        with_ts = [m for m in members if m.proxy_health_checked_at]
        if not with_ts:
            return None, None, None
        best = max(with_ts, key=lambda m: (m.proxy_health_checked_at or ""))
        return best.proxy_health_ok, best.proxy_health_checked_at, best.proxy_health_message

    def _proxies_table_item(self, text: str, *, editable: bool) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        flags = item.flags()
        if editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        else:
            flags &= ~Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        return item

    def _proxy_cell_to_optional(self, raw: str) -> str | None:
        s = (raw or "").strip()
        if not s or s == "—":
            return None
        return s

    def _proxy_batch_check_in_progress(self) -> bool:
        if self._proxy_health_thread and self._proxy_health_thread.isRunning():
            return True
        if self._import_health_thread and self._import_health_thread.isRunning():
            return True
        return False

    def _sync_proxies_page_buttons(self) -> None:
        if not hasattr(self, "btn_check_all_proxies"):
            return
        groups = self._group_profiles_by_proxy()
        busy = self._proxy_batch_check_in_progress()
        has_proxies = bool(self._group_profiles_by_proxy())
        self.btn_check_all_proxies.setEnabled(has_proxies and not busy)
        self.btn_refresh_proxies_table.setEnabled(not busy)
        if busy:
            self.btn_check_all_proxies.setText("Проверка…")
        else:
            self.btn_check_all_proxies.setText("Проверить все прокси")
        if hasattr(self, "table_proxies"):
            if busy:
                self.table_proxies.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            else:
                self.table_proxies.setEditTriggers(
                    QAbstractItemView.EditTrigger.DoubleClicked
                    | QAbstractItemView.EditTrigger.SelectedClicked
                    | QAbstractItemView.EditTrigger.EditKeyPressed,
                )
            for row in range(self.table_proxies.rowCount()):
                cell_btn = self.table_proxies.cellWidget(row, _PROXY_COL_REFRESH)
                if isinstance(cell_btn, QPushButton):
                    cell_btn.setEnabled(not busy)

    def _proxy_group_status_sort_key(self, members: list[BrowserProfile]) -> int:
        ok, _, _ = self._newest_health_in_group(members)
        if ok is True:
            return 0
        if ok is False:
            return 1
        return 2

    def _proxies_search_tokens(self) -> list[str]:
        raw = (self.ed_proxies_search.text() if hasattr(self, "ed_proxies_search") else "") or ""
        return [t for t in raw.lower().strip().split() if t]

    def _proxy_group_search_rank(
        self,
        key: tuple[str, str | None, str | None],
        members: list[BrowserProfile],
        tokens: list[str],
    ) -> int | None:
        """
        Ранг совпадения для сортировки: 0 — сервер, 1 — логин, 2 — ID профиля.
        None — строка не подходит (не все слова запроса найдены).
        """
        if not tokens:
            return 0
        srv, user, _pw = key
        srv_l = srv.lower()
        user_l = (user or "").lower()
        ids_l = [p.profile_id.lower() for p in members]
        worst_tier = 0
        for tok in tokens:
            if tok in srv_l:
                tier = 0
            elif tok in user_l:
                tier = 1
            elif any(tok in pid for pid in ids_l):
                tier = 2
            else:
                return None
            worst_tier = max(worst_tier, tier)
        return worst_tier

    def _filtered_proxy_groups(
        self,
        groups: dict[tuple[str, str | None, str | None], list[BrowserProfile]],
    ) -> list[tuple[int, tuple[str, str | None, str | None], list[BrowserProfile]]]:
        tokens = self._proxies_search_tokens()
        rows: list[tuple[int, tuple[str, str | None, str | None], list[BrowserProfile]]] = []
        for key, members in groups.items():
            rank = self._proxy_group_search_rank(key, members, tokens)
            if rank is None:
                continue
            rows.append((rank, key, members))
        rows.sort(
            key=lambda row: (
                row[0],
                self._proxy_group_status_sort_key(row[2]),
                row[1][0].lower(),
            ),
        )
        return rows

    def _update_proxies_page_stats(self, groups: dict[tuple[str, str | None, str | None], list[BrowserProfile]]) -> None:
        if not hasattr(self, "lbl_proxies_stat_total"):
            return
        total = len(groups)
        ok_n = fail_n = unknown_n = 0
        for members in groups.values():
            ok, _, _ = self._newest_health_in_group(members)
            if ok is True:
                ok_n += 1
            elif ok is False:
                fail_n += 1
            else:
                unknown_n += 1
        self.lbl_proxies_stat_total.setText(str(total))
        self.lbl_proxies_stat_ok.setText(str(ok_n))
        self.lbl_proxies_stat_fail.setText(str(fail_n))
        self.lbl_proxies_stat_unknown.setText(str(unknown_n))

    def _refresh_proxies_page_table(self) -> None:
        if not hasattr(self, "table_proxies"):
            return
        all_groups = self._group_profiles_by_proxy()
        visible_rows = self._filtered_proxy_groups(all_groups)
        visible_groups = {key: members for _rank, key, members in visible_rows}
        self._update_proxies_page_stats(visible_groups)
        self._proxies_table_refreshing = True
        self.table_proxies.blockSignals(True)
        self.table_proxies.setRowCount(0)
        muted = QColor("#94a3b8")
        ok_color = QColor("#86efac")
        fail_color = QColor("#fda4af")
        for _rank, key, members in visible_rows:
            srv, u, pw = key
            row = self.table_proxies.rowCount()
            self.table_proxies.insertRow(row)
            self.table_proxies.setRowHeight(row, 44)
            rep = members[0]
            ok, ts, msg = self._newest_health_in_group(members)
            if ok is True:
                status_label, sort_key, status_color = "● Рабочий", 0, ok_color
            elif ok is False:
                status_label, sort_key, status_color = "● Нерабочий", 1, fail_color
            else:
                status_label, sort_key, status_color = "● Не проверен", 2, muted
            tip = (msg or "").strip()
            if len(tip) > 900:
                tip = tip[:900] + "…"
            st_item = ProxyStatusTableItem(status_label, sort_key)
            st_item.setToolTip(tip if tip else status_label)
            st_item.setForeground(QBrush(status_color))
            st_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            st_item.setFlags(st_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            srv_item = self._proxies_table_item(srv, editable=True)
            srv_item.setToolTip("Двойной клик для редактирования. Изменение применится ко всем профилям в строке.")
            srv_item.setData(
                Qt.ItemDataRole.UserRole,
                {"member_ids": [p.profile_id for p in members], "proxy_key": key},
            )
            user_item = self._proxies_table_item(u or "", editable=True)
            pass_item = self._proxies_table_item(ProxyPasswordDelegate.mask_password(pw), editable=True)
            pass_item.setData(ProxyPasswordDelegate.REAL_PASSWORD_ROLE, pw or "")
            pass_item.setToolTip("Двойной клик — изменить пароль (в ячейке •••, при вводе — текст)")
            ids_widget = _ProxyProfileIdsCell(
                members,
                on_open=self._open_profile_from_proxies_tab,
                on_copy=self._copy_profile_id_to_clipboard,
                parent=self.table_proxies,
            )
            ts_item = self._proxies_table_item(ts or "—", editable=False)
            n_profiles = len(members)
            count_item = self._proxies_table_item(str(n_profiles), editable=False)
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            count_item.setToolTip(
                f"Профилей с этим прокси: {n_profiles}"
                if n_profiles != 1
                else "Один профиль с этим прокси",
            )
            self.table_proxies.setItem(row, _PROXY_COL_SERVER, srv_item)
            self.table_proxies.setItem(row, _PROXY_COL_LOGIN, user_item)
            self.table_proxies.setItem(row, _PROXY_COL_PASSWORD, pass_item)
            self.table_proxies.setCellWidget(row, _PROXY_COL_IDS, ids_widget)
            self.table_proxies.setItem(row, _PROXY_COL_STATUS, st_item)
            self.table_proxies.setItem(row, _PROXY_COL_CHECKED, ts_item)
            self.table_proxies.setItem(row, _PROXY_COL_COUNT, count_item)
            btn = QPushButton()
            btn.setObjectName("proxiesTableRefreshBtn")
            btn.setFixedSize(40, 32)
            btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            btn.setToolTip("Проверить этот прокси")
            btn.setEnabled(not self._proxy_batch_check_in_progress())
            btn.clicked.connect(lambda _c=False, pid=rep.profile_id: self._on_proxies_table_refresh_click(pid))
            self.table_proxies.setCellWidget(row, _PROXY_COL_REFRESH, btn)
            ids_widget.adjustSize()
            self.table_proxies.setRowHeight(row, max(52, ids_widget.sizeHint().height() + 10))
        self.table_proxies.blockSignals(False)
        self._proxies_table_refreshing = False
        self._sync_proxies_page_buttons()

    def _on_proxies_table_cell_changed(self, row: int, col: int) -> None:
        if self._proxies_table_refreshing:
            return
        if col not in (_PROXY_COL_SERVER, _PROXY_COL_LOGIN, _PROXY_COL_PASSWORD):
            return
        if self._proxy_batch_check_in_progress():
            return
        self._apply_proxies_table_row_edit(row)

    def _apply_proxies_table_row_edit(self, row: int) -> None:
        srv_item = self.table_proxies.item(row, _PROXY_COL_SERVER)
        if not srv_item:
            return
        meta = srv_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(meta, dict):
            return
        member_ids = meta.get("member_ids")
        if not isinstance(member_ids, list) or not member_ids:
            return

        raw_srv = (srv_item.text() or "").strip()
        login_item = self.table_proxies.item(row, _PROXY_COL_LOGIN)
        pass_item = self.table_proxies.item(row, _PROXY_COL_PASSWORD)
        new_user = self._proxy_cell_to_optional(login_item.text() if login_item else "")
        raw_pass = pass_item.data(ProxyPasswordDelegate.REAL_PASSWORD_ROLE) if pass_item else None
        new_pass = self._proxy_cell_to_optional("" if raw_pass is None else str(raw_pass))

        if not raw_srv:
            QMessageBox.warning(self, "Прокси", "Адрес сервера не может быть пустым.")
            self._refresh_proxies_page_table()
            return

        new_srv = normalize_proxy_server_url(raw_srv)
        if new_srv != raw_srv:
            self._proxies_table_refreshing = True
            self.table_proxies.blockSignals(True)
            srv_item.setText(new_srv)
            self.table_proxies.blockSignals(False)
            self._proxies_table_refreshing = False

        id_set = set(member_ids)
        members = [p for p in self._profiles if p.profile_id in id_set]
        if not members:
            self._refresh_proxies_page_table()
            return
        rep = members[0]
        new_key = canonical_proxy_key(new_srv, new_user, new_pass)
        old_key = canonical_proxy_key(rep.proxy_server, rep.proxy_username, rep.proxy_password)
        if new_key == old_key:
            return

        new_list: list[BrowserProfile] = []
        for p in self._profiles:
            if p.profile_id in id_set:
                base = replace(
                    p,
                    proxy_health_ok=None,
                    proxy_health_checked_at=None,
                    proxy_health_message=None,
                )
                new_list.append(
                    apply_proxy_and_sync_geo(
                        base,
                        proxy_server=new_srv,
                        proxy_username=new_user,
                        proxy_password=new_pass,
                    )
                )
            else:
                new_list.append(p)
        self._profiles = new_list
        save_profiles(self._profiles)
        self._refresh_profiles_list()
        if self._active_profile_id in id_set:
            self._load_active_profile_into_form()
        self._refresh_proxies_page_table()

    def _on_proxies_table_refresh_click(self, representative_profile_id: str) -> None:
        if self._proxy_batch_check_in_progress():
            QMessageBox.information(self, "Прокси", "Дождитесь завершения текущей проверки.")
            return
        self._start_proxy_health_check(representative_profile_id)

    def _on_check_all_proxies_click(self) -> None:
        groups = self._group_profiles_by_proxy()
        if not groups:
            QMessageBox.information(self, "Прокси", "Нет профилей с настроенным прокси.")
            return
        if self._proxy_batch_check_in_progress():
            QMessageBox.information(self, "Прокси", "Уже выполняется проверка прокси.")
            return
        jobs: list[tuple[str, str, str | None, str | None]] = []
        for members in groups.values():
            rep = members[0]
            jobs.append((rep.profile_id, rep.proxy_server or "", rep.proxy_username, rep.proxy_password))
        self._start_proxies_page_batch_health(jobs)

    def _apply_all_proxies_health_payload(self, payload: dict) -> None:
        for rep_id, (ok, msg, ts) in payload.items():
            rep = next((x for x in self._profiles if x.profile_id == rep_id), None)
            if not rep:
                continue
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

    def _start_proxies_page_batch_health(self, jobs: list[tuple[str, str, str | None, str | None]]) -> None:
        if not jobs:
            return
        if self._proxy_batch_check_in_progress():
            return
        dlg = ProxyBatchCheckProgressDialog(
            self,
            len(jobs),
            window_title="Прокси",
            progress_caption="Проверка всех прокси",
        )
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._import_health_dialog = dlg
        dlg.show()
        QApplication.processEvents()
        self._sync_proxies_page_buttons()

        self._import_health_thread = BatchImportProxyHealthThread(jobs)

        def on_prog(cur: int, total: int) -> None:
            dlg.set_progress(cur, total)
            QApplication.processEvents()

        def on_done(payload: dict) -> None:
            dlg.hide()
            dlg.deleteLater()
            self._import_health_dialog = None
            self._import_health_thread = None
            self._apply_all_proxies_health_payload(payload)
            ok_n = sum(1 for ok, _msg, _ts in payload.values() if ok)
            fail_n = len(payload) - ok_n
            QMessageBox.information(
                self,
                "Прокси",
                f"Проверено уникальных прокси: {len(payload)}.\n"
                f"Рабочих: {ok_n}.\n"
                f"Нерабочих: {fail_n}.",
            )
            self._sync_proxies_page_buttons()

        self._import_health_thread.progress.connect(on_prog)
        self._import_health_thread.finished_payload.connect(on_done)
        self._import_health_thread.start()

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
        self._sync_proxies_page_buttons()
        self._proxy_health_thread.start()

    def _on_proxy_health_check_thread_cleanup(self) -> None:
        self._proxy_health_thread = None
        self._sync_proxy_health_badge()
        self._sync_proxies_page_buttons()

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
        if self._proxy_batch_check_in_progress():
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
        self._sync_proxies_page_buttons()

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
            self._sync_proxies_page_buttons()
            QMessageBox.information(
                self,
                "Импорт",
                f"Создано профилей: {created_count}. Проверка прокси завершена.{skipped_extra}",
            )

        self._import_health_thread.progress.connect(on_prog)
        self._import_health_thread.finished_payload.connect(on_done)
        self._import_health_thread.start()

    def _profiles_list_vscroll(self) -> int:
        return int(self.profiles_list.verticalScrollBar().value())

    def _set_profiles_list_vscroll(self, value: int) -> None:
        self.profiles_list.verticalScrollBar().setValue(value)

    def _set_current_profile_list_item(self, it: QListWidgetItem) -> None:
        """Текущий профиль в списке без смены позиции прокрутки."""
        scroll = self._profiles_list_vscroll()
        self.profiles_list.blockSignals(True)
        try:
            self.profiles_list.setCurrentItem(it, QItemSelectionModel.SelectionFlag.Current)
        finally:
            self.profiles_list.blockSignals(False)
        self._set_profiles_list_vscroll(scroll)

    def _refresh_profiles_list(self) -> None:
        list_scroll = self._profiles_list_vscroll()
        q_raw = (self.ed_profiles_search.text() if hasattr(self, "ed_profiles_search") else "") or ""
        tokens = [t for t in q_raw.lower().strip().split() if t]

        def matches(p: BrowserProfile) -> bool:
            if not tokens:
                return True
            pid = (p.profile_id or "").lower()
            name = (p.name or "").lower()
            desc = (p.description or "").lower()
            tags = ", ".join(p.tags or []).lower()
            custom = custom_data_to_json_text(p.custom_data).lower()
            proxy_srv = (p.proxy_server or "").lower()
            proxy_user = (p.proxy_username or "").lower()
            hay = " ".join([pid, name, desc, tags, custom, proxy_srv, proxy_user])
            return all(t in hay for t in tokens)

        def rank(p: BrowserProfile, original_index: int) -> tuple[int, int]:
            """
            Приоритет поиска:
              0) profile_id
              1) name
              2) description/tags
              3) proxy (сервер / логин)
            Чем меньше — тем выше в списке. Второй ключ — стабильность (старый порядок).
            """
            if not tokens:
                return (original_index, original_index)

            q = q_raw.lower().strip()
            pid = (p.profile_id or "").lower()
            name = (p.name or "").lower()
            desc = (p.description or "").lower()
            tags = ", ".join(p.tags or []).lower()
            proxy_srv = (p.proxy_server or "").lower()
            proxy_user = (p.proxy_username or "").lower()

            # Предпочтение "как ввели" (целиком) если пользователь вставил кусок ID/имени.
            if q and q in pid:
                return (0, original_index)
            if q and q in name:
                return (1, original_index)
            if q and (q in desc or q in tags):
                return (2, original_index)
            if q and (q in proxy_srv or q in proxy_user):
                return (3, original_index)

            # Иначе — по токенам.
            if all(t in pid for t in tokens):
                return (0, original_index)
            if all(t in name for t in tokens):
                return (1, original_index)
            if all((t in desc) or (t in tags) for t in tokens):
                return (2, original_index)
            if all((t in proxy_srv) or (t in proxy_user) for t in tokens):
                return (3, original_index)

            # Совпало “смешанно” по разным полям — ставим в самый низ среди найденных.
            return (4, original_index)

        existing_ids = {p.profile_id for p in self._profiles}
        # Preserve row selection across rebuild (clear() drops Qt item state; we restore from _checked_profile_ids).
        preserve_sel: set[str] = set(self._checked_profile_ids)
        for it in self.profiles_list.selectedItems():
            pid = it.data(Qt.ItemDataRole.UserRole)
            if pid is not None:
                s = str(pid).strip()
                if s:
                    preserve_sel.add(s)
        preserve_sel.intersection_update(existing_ids)
        self._checked_profile_ids = preserve_sel

        self.profiles_list.blockSignals(True)
        self.profiles_list.clear()
        self._run_buttons.clear()
        self._profile_id_to_item.clear()
        self._profile_id_to_checkbox.clear()
        self._profile_id_to_row.clear()
        self._profile_id_to_title_label.clear()
        self._profile_id_to_id_label.clear()
        self._profile_row_filter_widgets.clear()

        matched: list[tuple[int, BrowserProfile]] = []
        for i, p in enumerate(self._profiles):
            if matches(p):
                matched.append((i, p))

        matched.sort(key=lambda ip: rank(ip[1], ip[0]))

        for _i, p in matched:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, p.profile_id)
            it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self.profiles_list.addItem(it)
            self._profile_id_to_item[p.profile_id] = it

            row = QWidget()
            row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            is_active_row = p.profile_id == self._active_profile_id
            row.setObjectName("profileRowActive" if is_active_row else "profileRow")
            self._profile_id_to_row[p.profile_id] = row
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(10, 6, 10, 6)
            row_l.setSpacing(10)

            cb = QCheckBox()
            cb.setChecked(p.profile_id in self._checked_profile_ids)
            cb.setToolTip(
                "Отметить профиль. Удерживайте ЛКМ и ведите по квадратам — отметятся все на пути (Ctrl — добавить)."
            )
            self._profile_id_to_checkbox[p.profile_id] = cb
            cb.stateChanged.connect(lambda _state, pid=p.profile_id: self._on_profile_checkbox_state_changed(pid))
            cb.installEventFilter(self)

            title_row = QWidget()
            title_l = QHBoxLayout(title_row)
            title_l.setContentsMargins(0, 0, 0, 0)
            title_l.setSpacing(6)

            name_lbl = QLabel(p.name)
            name_lbl.setObjectName(
                "profileRowTitleActive" if is_active_row else "profileRowTitle"
            )
            self._profile_id_to_title_label[p.profile_id] = name_lbl
            name_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard)

            id_lbl = QLabel(f"({p.profile_id})")
            id_lbl.setObjectName(
                "profileRowIdActive" if is_active_row else "profileRowId"
            )
            id_lbl.setFont(QFont("Consolas", 9))
            self._profile_id_to_id_label[p.profile_id] = id_lbl
            id_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard)

            copy_id_btn = QPushButton()
            copy_id_btn.setObjectName("profileIdCopyBtn")
            copy_id_btn.setFixedSize(22, 22)
            copy_id_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_id_btn.setToolTip("Копировать ID в буфер")
            copy_id_btn.setText("⧉")
            copy_id_btn.setFont(QFont("Segoe UI Symbol", 10))
            copy_id_btn.clicked.connect(
                lambda _c=False, pid=p.profile_id: self._copy_profile_id_to_clipboard(pid)
            )

            title_l.addWidget(name_lbl, 0)
            title_l.addWidget(id_lbl, 0)
            title_l.addWidget(copy_id_btn, 0)
            title_l.addStretch(1)

            info = QWidget()
            info_l = QVBoxLayout(info)
            info_l.setContentsMargins(0, 0, 0, 0)
            info_l.setSpacing(4)
            info_l.addWidget(title_row, 0)
            desc_text = (p.description or "").strip()
            if desc_text:
                desc_lbl = QLabel(desc_text)
                desc_lbl.setObjectName("profileRowDesc")
                desc_lbl.setWordWrap(True)
                desc_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard)
                info_l.addWidget(desc_lbl, 0)
            if p.tags:
                tags_w = _make_profile_tags_widget(p.tags, info)
                tags_w.adjustSize()
                info_l.addWidget(tags_w, 0, Qt.AlignmentFlag.AlignLeft)
            info_l.addStretch(0)
            info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

            proxy_dot = QLabel("")
            proxy_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            proxy_dot.setFixedWidth(18)
            proxy_dot.setToolTip("")
            dot_text, dot_css, dot_tip = self._proxy_health_dot_ui(p)
            proxy_dot.setText(dot_text)
            proxy_dot.setStyleSheet(dot_css)
            proxy_dot.setToolTip(dot_tip)

            btn_run = QPushButton()
            self._run_buttons[p.profile_id] = btn_run
            btn_run.clicked.connect(lambda _checked=False, pid=p.profile_id: self._run_button_clicked(pid))
            self._sync_run_button(p.profile_id)

            row_l.addWidget(cb, 0)
            row_l.addWidget(info, 1)
            row_l.addWidget(proxy_dot, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_l.addWidget(btn_run, 0, Qt.AlignmentFlag.AlignRight)

            info.adjustSize()
            row.adjustSize()
            hint = row.minimumSizeHint()
            it.setSizeHint(QSize(hint.width(), hint.height() + 6))
            self.profiles_list.setItemWidget(it, row)
            self._register_profile_row_mouse_targets(row, cb, btn_run, copy_id_btn)

        self.profiles_list.blockSignals(False)
        self._apply_profile_list_selection_visuals()
        self._apply_active_profile_row_visuals()

        if self._active_profile_id:
            ac_it = self._profile_id_to_item.get(self._active_profile_id)
            if ac_it is not None:
                self._set_current_profile_list_item(ac_it)
        self._set_profiles_list_vscroll(list_scroll)
        self._sync_proxy_health_badge()

    def _register_profile_row_mouse_targets(
        self, row: QWidget, cb: QCheckBox, btn_run: QPushButton, copy_id_btn: QPushButton
    ) -> None:
        """Фильтр на виджеты строки: клики по тексту не должны попадать в QListWidget как выделение."""
        targets: list[QWidget] = [row]
        for ch in row.findChildren(QWidget):
            if ch is cb or ch is btn_run or ch is copy_id_btn:
                continue
            targets.append(ch)
        for w in targets:
            w.installEventFilter(self)
            self._profile_row_filter_widgets.add(w)

    def _profiles_list_mouse_filter_active(self, watched: object) -> bool:
        if not hasattr(self, "profiles_list") or not isinstance(watched, QWidget):
            return False
        if watched is self.profiles_list.viewport():
            return True
        if isinstance(watched, QCheckBox) and watched in self._profile_id_to_checkbox.values():
            return True
        if watched is self.profiles_list:
            return True
        return watched in self._profile_row_filter_widgets

    @staticmethod
    def _repolish_widget(w: QWidget) -> None:
        st = w.style()
        st.unpolish(w)
        st.polish(w)
        w.update()

    def _apply_active_profile_row_visuals(self) -> None:
        """Подсветка профиля, открытого в редакторе (не путать с отметками чекбоксов)."""
        active = self._active_profile_id
        for pid, row in self._profile_id_to_row.items():
            is_active = pid == active
            row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            row.setObjectName("profileRowActive" if is_active else "profileRow")
            self._repolish_widget(row)
        for pid, lbl in self._profile_id_to_title_label.items():
            lbl.setObjectName("profileRowTitleActive" if pid == active else "profileRowTitle")
            self._repolish_widget(lbl)
        for pid, lbl in self._profile_id_to_id_label.items():
            lbl.setObjectName("profileRowIdActive" if pid == active else "profileRowId")
            self._repolish_widget(lbl)

    def _update_checked_profiles_count_label(self) -> None:
        n = len(self._checked_profile_ids)
        self.lbl_checked_profiles_count.setText(f"Выделено: {n}")

    def _apply_profile_list_selection_visuals(self) -> None:
        existing = {p.profile_id for p in self._profiles}
        self._checked_profile_ids.intersection_update(existing)
        self._syncing_selection_check = True
        try:
            for _pid, cb in self._profile_id_to_checkbox.items():
                cb.blockSignals(True)
                cb.setChecked(_pid in self._checked_profile_ids)
                cb.blockSignals(False)
        finally:
            self._syncing_selection_check = False
        self._update_checked_profiles_count_label()

    def _on_profile_checkbox_state_changed(self, profile_id: str) -> None:
        if self._syncing_selection_check:
            return
        cb = self._profile_id_to_checkbox.get(profile_id)
        if cb is None:
            return
        if cb.isChecked():
            self._checked_profile_ids.add(profile_id)
        else:
            self._checked_profile_ids.discard(profile_id)
        self._update_checked_profiles_count_label()

    def _on_profiles_list_item_selection_changed(self) -> None:
        """Синхронизировать подсветку строк только с чекбоксами (клик по строке не меняет отметки)."""
        if self._syncing_selection_check:
            return
        self._apply_profile_list_selection_visuals()

    def _run_button_at_viewport_pos(self, pos) -> QPushButton | None:
        it = self.profiles_list.itemAt(pos)
        if not it:
            return None
        w = self.profiles_list.itemWidget(it)
        if not w:
            return None
        local = w.mapFrom(self.profiles_list.viewport(), pos)
        ch = w.childAt(local)
        if isinstance(ch, QPushButton):
            return ch
        return None

    def _checkbox_at_viewport_pos(self, pos) -> bool:
        it = self.profiles_list.itemAt(pos)
        if not it:
            return False
        w = self.profiles_list.itemWidget(it)
        if not w:
            return False
        local = w.mapFrom(self.profiles_list.viewport(), pos)
        ch = w.childAt(local)
        return isinstance(ch, QCheckBox)

    def _title_label_at_viewport_pos(self, pos) -> bool:
        it = self.profiles_list.itemAt(pos)
        if not it:
            return False
        w = self.profiles_list.itemWidget(it)
        if not w:
            return False
        local = w.mapFrom(self.profiles_list.viewport(), pos)
        ch = w.childAt(local)
        return isinstance(ch, QLabel) and ch.objectName() in ("profileRowTitle", "profileRowId")

    def _profile_row_at_viewport_pos(self, pos) -> int | None:
        idx = self.profiles_list.indexAt(pos)
        if not idx.isValid():
            return None
        return int(idx.row())

    def _pid_at_list_row(self, row: int) -> str | None:
        if row < 0 or row >= self.profiles_list.count():
            return None
        it = self.profiles_list.item(row)
        if not it:
            return None
        pid = it.data(Qt.ItemDataRole.UserRole)
        if pid is None:
            return None
        s = str(pid).strip()
        return s if s else None

    def _profile_id_at_viewport_pos(self, pos) -> str | None:
        r = self._profile_row_at_viewport_pos(pos)
        return self._pid_at_list_row(r) if r is not None else None

    def _lmb_select_recompute(self) -> None:
        existing = {p.profile_id for p in self._profiles}
        if self._lmb_select_additive:
            self._checked_profile_ids = (self._lmb_select_base | self._lmb_select_visited) & existing
        else:
            self._checked_profile_ids = set(self._lmb_select_visited) & existing
        self._apply_profile_list_selection_visuals()

    def _lmb_select_visit_row_range(self, r0: int, r1: int) -> None:
        lo, hi = (r0, r1) if r0 <= r1 else (r1, r0)
        n = self.profiles_list.count()
        lo = max(0, lo)
        hi = min(n - 1, hi)
        if lo > hi:
            return
        changed = False
        for r in range(lo, hi + 1):
            pid = self._pid_at_list_row(r)
            if pid and pid not in self._lmb_select_visited:
                self._lmb_select_visited.add(pid)
                changed = True
        if changed:
            self._lmb_select_recompute()

    def _lmb_select_begin_at_row(self, row: int, modifiers: Qt.KeyboardModifier) -> bool:
        if self._lmb_select_active:
            return False
        pid = self._pid_at_list_row(row)
        if not pid:
            return False
        self._lmb_select_active = True
        self._lmb_select_additive = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        self._lmb_select_base = set(self._checked_profile_ids) if self._lmb_select_additive else set()
        self._lmb_select_visited = set()
        self._lmb_select_last_row = row
        self.profiles_list.viewport().grabMouse()
        self._lmb_select_visit_row_range(row, row)
        return True

    def _row_for_profile_checkbox(self, cb: QCheckBox) -> int | None:
        for pid, box in self._profile_id_to_checkbox.items():
            if box is cb:
                it = self._profile_id_to_item.get(pid)
                if it is not None:
                    return self.profiles_list.row(it)
        return None

    def _toggle_profile_checkbox_row(self, row: int) -> None:
        pid = self._pid_at_list_row(row)
        if not pid:
            return
        if pid in self._checked_profile_ids:
            self._checked_profile_ids.discard(pid)
        else:
            self._checked_profile_ids.add(pid)
        self._apply_profile_list_selection_visuals()

    def _clear_checkbox_click_pending(self) -> None:
        self._checkbox_row_pending = None
        self._checkbox_press_global = None
        self._checkbox_press_modifiers = Qt.KeyboardModifier.NoModifier

    def _begin_checkbox_click_pending(
        self, row: int, global_pos: QPoint, modifiers: Qt.KeyboardModifier
    ) -> None:
        self._clear_checkbox_click_pending()
        self._checkbox_row_pending = row
        self._checkbox_press_global = global_pos
        self._checkbox_press_modifiers = modifiers
        try:
            self.profiles_list.viewport().grabMouse()
        except Exception:
            pass

    def _try_begin_checkbox_paint_from_pending(self, modifiers: Qt.KeyboardModifier) -> bool:
        row = self._checkbox_row_pending
        if row is None:
            return False
        self._clear_checkbox_click_pending()
        return self._lmb_select_begin_at_row(row, modifiers)

    def _lmb_select_update_hover(self, viewport_pos) -> None:
        if not self._lmb_select_active:
            return
        # Do not skip over the "Run" column here: indexAt still maps to the row; otherwise fast
        # diagonal drags miss rows. Run is ignored only on press (try_begin).
        cur = self._profile_row_at_viewport_pos(viewport_pos)
        if cur is None:
            return
        last = self._lmb_select_last_row
        self._lmb_select_last_row = cur
        if last is None:
            self._lmb_select_visit_row_range(cur, cur)
        else:
            self._lmb_select_visit_row_range(last, cur)

    def _lmb_select_end(self, viewport_pos) -> None:
        if not self._lmb_select_active:
            return
        self._lmb_select_active = False
        self._lmb_select_additive = False
        self._lmb_select_base.clear()
        self._lmb_select_visited.clear()
        self._lmb_select_last_row = None
        try:
            self.profiles_list.viewport().releaseMouse()
        except Exception:
            pass

    def _finish_checkbox_click_without_drag(self) -> None:
        try:
            self.profiles_list.viewport().releaseMouse()
        except Exception:
            pass
        row = self._checkbox_row_pending
        self._clear_checkbox_click_pending()
        if row is None or self._lmb_select_active:
            return
        self._toggle_profile_checkbox_row(row)

    def _clear_profile_title_click_pending(self) -> None:
        self._title_row_pending = None
        self._title_press_global = None
        self._title_press_modifiers = Qt.KeyboardModifier.NoModifier

    def _finish_profile_title_click_without_drag(self) -> None:
        try:
            self.profiles_list.viewport().releaseMouse()
        except Exception:
            pass
        row = self._title_row_pending
        self._clear_profile_title_click_pending()
        if row is None or self._lmb_select_active:
            return
        self._open_profile_row_editor(row)

    def _open_profile_row_editor(self, row: int) -> None:
        pid = self._pid_at_list_row(row)
        if not pid:
            return
        it = self._profile_id_to_item.get(pid)
        if it is None:
            return
        self._active_profile_id = pid
        self._apply_active_profile_row_visuals()
        self._set_current_profile_list_item(it)
        self._load_active_profile_into_form()

    def _batch_profile_ids(self) -> list[str]:
        """
        IDs for batch actions (launch / delete / export): union of checked rows and Qt list selection.
        Order follows the visible list top-to-bottom, then any ids not currently visible (e.g. search filter).
        """
        want: set[str] = set(self._checked_profile_ids)
        want.update(self._selected_profile_ids())
        if not want:
            return []
        ordered = [pid for pid in self._profile_ids_in_list_widget_order() if pid in want]
        for pid in want:
            if pid not in ordered:
                ordered.append(pid)
        return ordered

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

    def _sync_profile_from_disk(self, profile_id: str) -> None:
        """
        UI-sync entrypoint used by the local API hook.
        Debounces full reload so bursts of API metadata updates do not freeze the UI.
        """
        self._sync_run_button(profile_id)
        self._metadata_sync_timer.start()

    def _flush_metadata_sync_from_disk(self) -> None:
        prev_active = self._active_profile_id
        prev_checked = set(self._checked_profile_ids)
        self._profiles = load_profiles()

        existing_ids = {p.profile_id for p in self._profiles}
        self._checked_profile_ids = prev_checked.intersection(existing_ids)
        if prev_active and prev_active in existing_ids:
            self._active_profile_id = prev_active
        else:
            self._active_profile_id = self._profiles[0].profile_id if self._profiles else None

        self._refresh_profiles_list()
        if self._active_profile_id:
            it = self._profile_id_to_item.get(self._active_profile_id)
            if it is not None:
                self._set_current_profile_list_item(it)
        self._load_active_profile_into_form()

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
            self._apply_active_profile_row_visuals()
            return
        pid = current.data(Qt.ItemDataRole.UserRole)
        if not pid:
            self._active_profile_id = None
            self._apply_active_profile_row_visuals()
            return
        self._active_profile_id = str(pid)
        self._apply_active_profile_row_visuals()
        self._load_active_profile_into_form()

    def _profiles_list_mouse_vp_pos(self, watched: QWidget, event: QMouseEvent) -> QPoint:
        vp = self.profiles_list.viewport()
        return vp.mapFrom(watched, event.position().toPoint())

    def _profiles_list_handle_lmb_release(self, vp_pos: QPoint) -> bool:
        if self._lmb_select_active:
            self._lmb_select_end(vp_pos)
            return True
        if self._checkbox_row_pending is not None:
            self._finish_checkbox_click_without_drag()
            return True
        if self._title_row_pending is not None:
            self._finish_profile_title_click_without_drag()
            return True
        return False

    def _profiles_list_handle_lmb_move(self, event: QMouseEvent, vp_pos: QPoint) -> bool:
        if self._lmb_select_active:
            self._lmb_select_update_hover(vp_pos)
            return True
        if self._checkbox_row_pending is not None and self._checkbox_press_global is not None:
            dg = event.globalPosition().toPoint() - self._checkbox_press_global
            if abs(dg.x()) + abs(dg.y()) >= 5:
                self._try_begin_checkbox_paint_from_pending(self._checkbox_press_modifiers)
            return True
        if self._title_row_pending is not None and self._title_press_global is not None:
            dg = event.globalPosition().toPoint() - self._title_press_global
            if abs(dg.x()) + abs(dg.y()) >= 5:
                self._clear_profile_title_click_pending()
            return True
        return False

    def eventFilter(self, watched: object, event: object) -> bool:  # type: ignore[override]
        # Чекбокс: клик — переключить; с перетаскиванием — «краска». Строка — открыть настройки.
        try:
            if not self._profiles_list_mouse_filter_active(watched):
                return super().eventFilter(watched, event)
            if isinstance(watched, QScrollBar):
                return super().eventFilter(watched, event)
            if isinstance(watched, QPushButton) and (
                (self._run_buttons and any(watched is b for b in self._run_buttons.values()))
                or watched.objectName() == "profileIdCopyBtn"
            ):
                return super().eventFilter(watched, event)
            if not isinstance(event, QMouseEvent):
                return super().eventFilter(watched, event)

            if not isinstance(watched, QWidget):
                return super().eventFilter(watched, event)
            vp_pos = self._profiles_list_mouse_vp_pos(watched, event)
            et = event.type()

            profile_cb = (
                isinstance(watched, QCheckBox) and watched in self._profile_id_to_checkbox.values()
            )

            if et == event.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                if self._profiles_list_handle_lmb_release(vp_pos):
                    return True
                return False

            if et == event.Type.MouseMove and (event.buttons() & Qt.MouseButton.LeftButton):
                if self._profiles_list_handle_lmb_move(event, vp_pos):
                    return True
                return False

            if et == event.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                if profile_cb:
                    row = self._row_for_profile_checkbox(watched)
                    if row is not None:
                        self._begin_checkbox_click_pending(
                            row, event.globalPosition().toPoint(), event.modifiers()
                        )
                        return True
                    return False
                if self._run_button_at_viewport_pos(vp_pos):
                    return False
                if self._checkbox_at_viewport_pos(vp_pos):
                    row = self._profile_row_at_viewport_pos(vp_pos)
                    if row is not None:
                        self._begin_checkbox_click_pending(
                            row, event.globalPosition().toPoint(), event.modifiers()
                        )
                        return True
                    return False
                row = self._profile_row_at_viewport_pos(vp_pos)
                if row is not None:
                    self._title_row_pending = row
                    self._title_press_global = event.globalPosition().toPoint()
                    self._title_press_modifiers = event.modifiers()
                    try:
                        self.profiles_list.viewport().grabMouse()
                    except Exception:
                        pass
                    return True
                return False
        except Exception:
            pass
        return super().eventFilter(watched, event)

    def _clear_profiles_list_selection(self) -> None:
        """Снять все отметки и выделение строк; прервать «краску» / ожидание клика по подписи, если были."""
        if self._lmb_select_active:
            self._lmb_select_active = False
            self._lmb_select_additive = False
            self._lmb_select_base.clear()
            self._lmb_select_visited.clear()
            self._lmb_select_last_row = None
            try:
                self.profiles_list.viewport().releaseMouse()
            except Exception:
                pass
        if self._title_row_pending is not None:
            try:
                self.profiles_list.viewport().releaseMouse()
            except Exception:
                pass
            self._clear_profile_title_click_pending()
        if self._checkbox_row_pending is not None:
            try:
                self.profiles_list.viewport().releaseMouse()
            except Exception:
                pass
            self._clear_checkbox_click_pending()
        self._checked_profile_ids.clear()
        self.profiles_list.blockSignals(True)
        self.profiles_list.clearSelection()
        self.profiles_list.blockSignals(False)
        self._apply_profile_list_selection_visuals()

    def _load_active_profile_into_form(self) -> None:
        p = self._active_profile()
        if not p:
            self._editable_tags = []
            self._rebuild_tag_chips()
            self.ed_tag_add.clear()
            self.ed_description.setPlainText("")
            self.ed_custom_data.setPlainText("")
            self._collapse_custom_data_panel()
            self._sync_proxy_health_badge()
            return
        self.ed_name.setText(p.name)
        self._editable_tags = list(p.tags)
        self._rebuild_tag_chips()
        self.ed_tag_add.clear()
        self.ed_description.setPlainText((p.description or "").replace("\r\n", "\n"))
        self.ed_custom_data.setPlainText(custom_data_to_json_text(p.custom_data))
        self._collapse_custom_data_panel()
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

    def _normalize_proxy_server_field_in_place(self) -> None:
        raw = (self.ed_proxy_server.text() or "").strip()
        if not raw:
            return
        canon = normalize_proxy_server_url(raw)
        if canon != raw:
            self.ed_proxy_server.blockSignals(True)
            self.ed_proxy_server.setText(canon)
            self.ed_proxy_server.blockSignals(False)

    def _on_proxy_fields_edited(self) -> None:
        """
        When proxy changes, regenerate fingerprint/persona fields in the form.
        Changes are not persisted until user clicks 'Save profile'.
        """
        self._normalize_proxy_server_field_in_place()
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

    def _export_profiles_archive(self) -> None:
        if self._archive_export_thread and self._archive_export_thread.isRunning():
            return
        if self._archive_import_thread and self._archive_import_thread.isRunning():
            QMessageBox.information(self, "Экспорт", "Дождитесь завершения импорта архива.")
            return

        ids = self._batch_profile_ids()
        if ids:
            want: set[str] = set(ids)
            to_export = [p for p in self._profiles if p.profile_id in want]
        else:
            to_export = list(self._profiles)

        if not to_export:
            QMessageBox.information(self, "Экспорт", "Нет профилей для экспорта.")
            return

        running = [p.profile_id for p in to_export if self._is_profile_running(p.profile_id)]
        if running:
            QMessageBox.warning(
                self,
                "Экспорт",
                "Закройте браузер для профилей, которые попадут в архив, и повторите попытку:\n"
                + "\n".join(running[:15])
                + ("\n…" if len(running) > 15 else ""),
            )
            return

        opt_dlg = ExportProfilesOptionsDialog(self, len(to_export))
        if opt_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        cookie_hosts: set[str] | None = None
        if opt_dlg.cookies_only:
            scan_dlg = QProgressDialog("Сканирование сайтов в cookies…", None, 0, 0, self)
            scan_dlg.setWindowTitle("Экспорт профилей")
            scan_dlg.setWindowModality(Qt.WindowModality.WindowModal)
            scan_dlg.setMinimumDuration(0)
            scan_dlg.setCancelButton(None)
            scan_dlg.show()
            QApplication.processEvents()
            try:
                from cookies_io import collect_hosts_for_profiles

                hosts = collect_hosts_for_profiles([p.profile_id for p in to_export])
            except Exception as e:
                scan_dlg.close()
                QMessageBox.warning(self, "Экспорт", str(e).strip() or "Не удалось прочитать cookies")
                return
            scan_dlg.close()

            if not hosts:
                QMessageBox.information(
                    self,
                    "Экспорт",
                    "У выбранных профилей нет сохранённых cookies.",
                )
                return

            host_dlg = CookieHostsSelectDialog(self, hosts)
            if host_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            cookie_hosts = host_dlg.selected_hosts()
            if not cookie_hosts:
                QMessageBox.warning(self, "Экспорт", "Выберите хотя бы один сайт.")
                return

        dest = QFileDialog.getExistingDirectory(self, "Папка для сохранения архива")
        if not dest:
            return

        dlg = QProgressDialog(
            "Создание архива cookies…" if opt_dlg.cookies_only else "Создание архива…",
            None,
            0,
            0,
            self,
        )
        dlg.setWindowTitle("Экспорт профилей")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setCancelButton(None)
        dlg.show()
        QApplication.processEvents()

        self._archive_export_thread = ProfilesArchiveExportThread(
            dest,
            list(to_export),
            cookies_only=opt_dlg.cookies_only,
            cookie_hosts=cookie_hosts,
        )

        def on_prog(msg: str) -> None:
            dlg.setLabelText(msg)
            QApplication.processEvents()

        def on_ok(path: str) -> None:
            self._archive_export_thread = None
            dlg.reset()
            dlg.deleteLater()
            QMessageBox.information(self, "Экспорт", f"Архив сохранён:\n{path}")

        def on_fail(msg: str) -> None:
            self._archive_export_thread = None
            dlg.reset()
            dlg.deleteLater()
            QMessageBox.warning(self, "Экспорт", msg)

        self._archive_export_thread.progress.connect(on_prog)
        self._archive_export_thread.finished_ok.connect(on_ok)
        self._archive_export_thread.failed.connect(on_fail)
        self._archive_export_thread.start()

    def _import_profiles_archive(self) -> None:
        if self._archive_import_thread and self._archive_import_thread.isRunning():
            return
        if self._archive_export_thread and self._archive_export_thread.isRunning():
            QMessageBox.information(self, "Импорт", "Дождитесь завершения экспорта архива.")
            return
        if self._import_build_thread and self._import_build_thread.isRunning():
            QMessageBox.information(self, "Импорт", "Дождитесь завершения другого импорта.")
            return

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Архив профилей",
            "",
            "ZIP архив (*.zip)",
        )
        if not path:
            return

        dlg = QProgressDialog("Импорт архива…", None, 0, 0, self)
        dlg.setWindowTitle("Импорт профилей")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setCancelButton(None)
        dlg.show()
        QApplication.processEvents()

        self._archive_import_thread = ProfilesArchiveImportThread(path, list(self._profiles))

        def on_prog(msg: str) -> None:
            dlg.setLabelText(msg)
            QApplication.processEvents()

        def on_ok(added: int, remapped: int) -> None:
            self._archive_import_thread = None
            dlg.reset()
            dlg.deleteLater()
            self._profiles = load_profiles()
            if self._profiles:
                if self._active_profile_id not in {p.profile_id for p in self._profiles}:
                    self._active_profile_id = self._profiles[0].profile_id
            else:
                self._active_profile_id = None
            self._refresh_profiles_list()
            self._load_active_profile_into_form()
            extra = f"\n\nПрофилей с новым ID из‑за конфликта имён: {remapped}." if remapped else ""
            QMessageBox.information(
                self,
                "Импорт",
                f"Добавлено профилей: {added}.{extra}",
            )

        def on_fail(msg: str) -> None:
            self._archive_import_thread = None
            dlg.reset()
            dlg.deleteLater()
            QMessageBox.warning(self, "Импорт", msg)

        self._archive_import_thread.progress.connect(on_prog)
        self._archive_import_thread.finished_ok.connect(on_ok)
        self._archive_import_thread.failed.connect(on_fail)
        self._archive_import_thread.start()

    def _selected_profile_ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for it in self.profiles_list.selectedItems():
            pid = it.data(Qt.ItemDataRole.UserRole)
            if pid is None:
                continue
            s = str(pid).strip()
            if not s:
                continue
            if s not in seen:
                seen.add(s)
                ids.append(s)
        return ids

    def _profile_ids_in_list_widget_order(self) -> list[str]:
        out: list[str] = []
        for i in range(self.profiles_list.count()):
            it = self.profiles_list.item(i)
            if not it:
                continue
            raw = it.data(Qt.ItemDataRole.UserRole)
            if raw is None:
                continue
            s = str(raw).strip()
            if not s:
                continue
            out.append(s)
        return out

    def _clear_profiles(self) -> None:
        ids = self._batch_profile_ids()
        if not ids and self._active_profile_id:
            ids = [self._active_profile_id]
        if not ids:
            QMessageBox.information(self, "Очистка", "Нет профилей для очистки.")
            return

        to_clear: list[BrowserProfile] = [p for p in self._profiles if p.profile_id in ids]
        if not to_clear:
            return

        running = [p.profile_id for p in to_clear if self._is_profile_running(p.profile_id)]
        if running:
            QMessageBox.warning(
                self,
                "Запущен профиль",
                "Нельзя очистить, пока браузер запущен. Остановите или закройте окна для:\n"
                + "\n".join(running),
            )
            return

        if len(to_clear) == 1:
            p0 = to_clear[0]
            msg = f"Профиль «{p0.name}»\n\nID: {p0.profile_id}"
        else:
            msg = f"Выбранные профили ({len(to_clear)} шт.):\n\n" + "\n".join(
                f"• {p.name} ({p.profile_id})" for p in to_clear[:20]
            )
            if len(to_clear) > 20:
                msg += f"\n… и ещё {len(to_clear) - 20}"

        dlg = _ClearProfilesOptionsDialog(self, msg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        change_fp = dlg.change_fingerprint
        clear_data = dlg.clear_browser_data

        confirm_parts: list[str] = []
        if clear_data:
            confirm_parts.append("удалены все данные браузера (кэш, куки, сессии)")
        if change_fp:
            confirm_parts.append("сгенерирован новый отпечаток (имя и прокси сохранятся)")
        confirm_parts.append("сброшены теги и пользовательские данные")
        confirm_msg = (
            "Будут " + " и ".join(confirm_parts) + ".\n\n"
            "Нажмите «Да» ещё раз для окончательного подтверждения."
        )
        if clear_data:
            confirm_msg += "\n\nДанные браузера будут удалены без возможности восстановления."

        if QMessageBox.question(self, "Подтверждение очистки", confirm_msg) != QMessageBox.StandardButton.Yes:
            return

        clear_ids = {p.profile_id for p in to_clear}
        for i, p in enumerate(self._profiles):
            if p.profile_id not in clear_ids:
                continue
            updated = p
            if clear_data:
                try:
                    shutil.rmtree(profile_user_data_dir(p.profile_id), ignore_errors=True)
                except Exception:
                    pass
            if change_fp:
                updated = regenerate_profile_fingerprint(updated)
            self._profiles[i] = replace(updated, tags=[], custom_data={})

        save_profiles(self._profiles)
        self._refresh_profiles_list()
        self._load_active_profile_into_form()

    def _delete_profile(self) -> None:
        ids = self._batch_profile_ids()
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
        self._checked_profile_ids.difference_update(remove_ids)
        if self._active_profile_id in remove_ids:
            self._active_profile_id = self._profiles[0].profile_id if self._profiles else None
        save_profiles(self._profiles)
        self._refresh_profiles_list()
        self._load_active_profile_into_form()

    def _save_active_profile(self) -> None:
        p = self._active_profile()
        if not p:
            return
        pr = (self.ed_proxy_server.text() or "").strip()
        proxy_server = normalize_proxy_server_url(pr) if pr else None
        if proxy_server and proxy_server != pr:
            self.ed_proxy_server.blockSignals(True)
            self.ed_proxy_server.setText(proxy_server)
            self.ed_proxy_server.blockSignals(False)
        no_proxy = not proxy_server
        proxy_user = self._blank_to_none(self.ed_proxy_user.text())
        proxy_pass = self._blank_to_none(self.ed_proxy_pass.text())
        proxy_changed = (
            (p.proxy_server or None) != proxy_server
            or (p.proxy_username or None) != proxy_user
            or (p.proxy_password or None) != proxy_pass
        )
        desc_raw = self.ed_description.toPlainText()
        desc_stripped = desc_raw.strip()
        custom_raw = self.ed_custom_data.toPlainText()
        try:
            custom_parsed = custom_data_from_json_text(custom_raw)
        except (json.JSONDecodeError, ValueError) as e:
            QMessageBox.warning(
                self,
                "Доп. данные",
                f"Некорректный JSON в поле «Доп. данные»:\n{e}",
            )
            return
        updated = replace(
            p,
            name=self.ed_name.text().strip() or p.name,
            tags=list(self._editable_tags),
            description=desc_stripped if desc_stripped else None,
            custom_data=custom_parsed,
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
        ids = self._batch_profile_ids()
        if not ids:
            QMessageBox.information(
                self,
                "Выбор профилей",
                "Отметьте чекбоксами или выделите один/несколько профилей для запуска.",
            )
            return
        self._launch_profiles(ids)

    def _launch_profiles(self, ids: list[str]) -> None:
        url = self.ed_url.text().strip() or "https://studio.youtube.com"
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
            set_ui_profile_running(profile_id, True)
            runner.start()
            self._sync_run_button(profile_id)

    def _on_runner_finished(self, profile_id: str, prefix: str, ok: bool, msg: str) -> None:
        self._append_log(f"{prefix} finished: {'OK' if ok else 'FAIL'} — {msg}")
        r = self._runners.get(profile_id)
        tid = getattr(r, "api_tracked_session_id", None) if r else None
        if r and not r.isRunning():
            self._runners.pop(profile_id, None)
        set_ui_profile_running(profile_id, False)
        if isinstance(tid, str) and tid:
            notify_ui_session_finished(tid, ok, msg)
        self._sync_run_button(profile_id)

    def _append_log(self, s: str) -> None:
        self.log.appendPlainText(s)


def _offer_json_migration_dialog() -> None:
    if not needs_json_migration():
        return

    count = count_legacy_json_profiles()
    json_path = legacy_json_path()
    reply = QMessageBox.question(
        None,
        "Миграция базы данных",
        (
            "Обнаружен старый файл profiles.json с данными профилей.\n\n"
            f"Файл: {json_path}\n"
            f"Профилей: {count}\n\n"
            "Перенести все данные в новую базу SQLite?\n"
            "После миграции profiles.json будет переименован в profiles.json.migrated."
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if reply != QMessageBox.StandardButton.Yes:
        return

    try:
        migrated = migrate_json_to_sqlite()
    except Exception as e:
        QMessageBox.critical(
            None,
            "Ошибка миграции",
            f"Не удалось перенести данные из profiles.json:\n{e}",
        )
        return

    QMessageBox.information(
        None,
        "Миграция завершена",
        f"Перенесено профилей: {migrated}.\nДанные сохранены в SQLite.",
    )


def run_qt() -> None:
    app = QApplication([])
    app.setApplicationName("Antidetect UI")
    _ico = build_app_icon()
    app.setWindowIcon(_ico)
    app.setStyleSheet(ZALIVER_DARK_QSS)
    _offer_json_migration_dialog()
    w = MainWindow()
    w.setWindowIcon(_ico)
    base = start_profile_api_background()
    if base:
        w._append_log(f"[API] локальный сервер: {base}/docs")
    # Open maximized ("full screen" for typical desktop usage).
    w.showMaximized()
    app.exec()

