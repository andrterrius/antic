from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import socket
import tempfile
import time
from pathlib import Path
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, List, Literal

if sys.version_info < (3, 10):
    import eval_type_backport  # noqa: F401  # Pydantic: list[str], str | None on 3.8–3.9

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask

from collections.abc import Callable

from api_auth import auth_from_request, get_configured_token, require_api_token
from auth_routes import build_auth_router
from auth_runtime import get_users_store
from profiles_store import (
    BrowserProfile,
    get_profile,
    load_profiles,
    set_data_username,
    user_data_scope,
    normalize_custom_data,
    normalize_tags_list,
    save_profiles,
    update_profile_custom_data,
    update_profile_name,
    update_profile_tags,
    set_profiles_ui_log_hook,
)
from playwright_runner import (
    chromium_user_data_parent,
    normalize_cdp_public_host,
    rewrite_cdp_public_urls,
    run_profile,
)
from static_ui import resolve_web_dist


# --- OpenAPI / Pydantic-схемы (документация в /docs) ---


class ProfileOut(BaseModel):
    """Сохранённый профиль браузера (настройки Playwright + отпечаток)."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(..., description="Уникальный идентификатор профиля")
    name: str = Field(..., description="Имя профиля в списке")
    tags: list[str] = Field(default_factory=list, description="Теги профиля (произвольное число)")
    description: str | None = Field(None, description="Текстовое описание профиля")
    custom_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Произвольные JSON-совместимые данные (ключ → значение)",
    )
    automation_enabled: bool = Field(False, description="Флаг автоматизации (по смыслу приложения)")
    proxy_server: str | None = Field(
        None,
        description="Прокси: host:port (по умолчанию http), либо http://… / socks5://…",
    )
    proxy_username: str | None = Field(None)
    proxy_password: str | None = Field(None)
    proxy_health_ok: bool | None = Field(None, description="Результат последней проверки прокси")
    proxy_health_checked_at: str | None = Field(None, description="Время проверки прокси (UTC ISO)")
    proxy_health_message: str | None = Field(None)
    engine: str | None = Field("chromium", description="Движок: chromium | firefox | webkit")
    device_preset: str | None = Field(None, description="Пресет устройства Playwright, напр. iPhone 13")
    user_agent: str | None = Field(None)
    locale: str | None = Field(None)
    timezone_id: str | None = Field(None)
    country_code: str | None = Field(None, description="ISO-3166 alpha-2")
    viewport_width: int | None = Field(None)
    viewport_height: int | None = Field(None)
    color_scheme: str | None = Field(None, description="light | dark | no-preference")
    geo_lat: float | None = Field(None)
    geo_lon: float | None = Field(None)
    webgl_vendor: str | None = Field(None)
    webgl_renderer: str | None = Field(None)
    webgl_version: str | None = Field(None)
    webgl_shading_language_version: str | None = Field(None)
    running: bool = Field(
        False,
        description="True, если профиль сейчас запущен (API или UI)",
    )


_PROFILE_NAME_MAX_LEN = 256


class ExportProfilesBody(BaseModel):
    """Экспорт выбранных профилей в ZIP."""

    model_config = ConfigDict(extra="forbid")

    profile_ids: list[str] = Field(
        default_factory=list,
        description="ID профилей; пустой список — все профили",
    )
    mode: Literal["full", "cookies"] = Field(
        "full",
        description="full — user-data; cookies — только cookies выбранных доменов",
    )
    hosts: list[str] = Field(
        default_factory=list,
        description="Домены для mode=cookies (обязательно непустой при cookies)",
    )


class CookieHostsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_ids: list[str] = Field(
        default_factory=list,
        description="ID профилей для сканирования cookie-хостов",
    )


class CookieHostOut(BaseModel):
    host: str
    count: int


class CookieHostsOut(BaseModel):
    hosts: list[CookieHostOut] = Field(default_factory=list)


class ImportProfilesOut(BaseModel):
    imported: int = Field(..., description="Сколько профилей добавлено")
    remapped: int = Field(..., description="Сколько ID переназначено из-за конфликта")
    total: int = Field(..., description="Всего профилей в базе после импорта")


class DeleteProfilesBody(BaseModel):
    """Удаление выбранных профилей."""

    model_config = ConfigDict(extra="forbid")

    profile_ids: list[str] = Field(
        ...,
        min_length=1,
        description="ID профилей для удаления",
    )
    purge_data: bool = Field(
        True,
        description="Удалить каталог user-data профиля (как в десктопном UI)",
    )


class DeleteProfilesOut(BaseModel):
    deleted: int = Field(..., description="Сколько профилей удалено")
    deleted_ids: list[str] = Field(default_factory=list, description="Удалённые profile_id")
    total: int = Field(..., description="Всего профилей в базе после удаления")


class ProxyProfileRef(BaseModel):
    profile_id: str
    name: str


class ProxyGroupOut(BaseModel):
    proxy_server: str
    proxy_username: str | None = None
    proxy_password: str | None = None
    profile_count: int
    profiles: list[ProxyProfileRef] = Field(default_factory=list)
    health_ok: bool | None = None
    health_checked_at: str | None = None
    health_message: str | None = None


class ProxyCheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy_server: str = Field(..., min_length=1)
    proxy_username: str | None = None
    proxy_password: str | None = None


class ProxyUpdateBody(BaseModel):
    """Изменить прокси у всех профилей с теми же credentials."""

    model_config = ConfigDict(extra="forbid")

    match_proxy_server: str = Field(..., min_length=1, description="Текущий proxy_server группы")
    match_proxy_username: str | None = None
    match_proxy_password: str | None = None
    proxy_server: str = Field(..., min_length=1, description="Новый адрес прокси")
    proxy_username: str | None = None
    proxy_password: str | None = None


class ProxyUpdateOut(BaseModel):
    updated: int
    group: ProxyGroupOut


class ProxyCheckAllOut(BaseModel):
    checked: int
    ok: int
    fail: int
    groups: list[ProxyGroupOut] = Field(default_factory=list)


class ProxyImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., description="Текст файла: строки host:port:user:pass")
    proxy_scheme: Literal["http", "socks5"] = Field("http")


class ProxyImportOut(BaseModel):
    created: int
    skipped: int
    profiles: list[ProfileOut] = Field(default_factory=list)


class ProfileNamePatch(BaseModel):
    """Частичное обновление профиля (сейчас — только имя)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Новое имя профиля")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("name must be non-empty")
        if len(s) > _PROFILE_NAME_MAX_LEN:
            raise ValueError(f"name is too long (max {_PROFILE_NAME_MAX_LEN})")
        return s


class CustomDataBody(BaseModel):
    """Полная замена или слияние custom_data профиля."""

    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Объект custom_data (строковые ключи, JSON-совместимые значения)",
    )


class CustomDataValueBody(BaseModel):
    """Значение одного ключа custom_data."""

    model_config = ConfigDict(extra="forbid")

    value: Any = Field(..., description="JSON-совместимое значение (строка, число, объект, массив, null)")


class LaunchProfileBody(BaseModel):
    """Тело запроса на запуск профиля по HTTP."""

    headless: bool = Field(False, description="Запуск без окна (headless Chromium)")
    expose_cdp: bool = Field(
        True,
        description="Выделить порт remote debugging и заполнить cdp_ws_url в сессии (для connect_over_cdp)",
    )
    cdp_port: int | None = Field(
        None,
        ge=1024,
        le=65535,
        description="Фиксированный порт CDP (по умолчанию — свободный случайный).",
    )
    cdp_bind: Literal["loopback", "all"] = Field(
        "loopback",
        description="loopback=127.0.0.1; all=внешний доступ через socat на cdp_port (Chromium слушает только localhost).",
    )
    cdp_public_host: str | None = Field(
        None,
        description="Публичный IP или домен в cdp_ws_url (без http://; обязателен при cdp_bind=all).",
    )
    start_url: str = Field(
        default="https://studio.youtube.com",
        max_length=4096,
        description="Первая открываемая страница после старта контекста",
    )
    script_path: str | None = Field(
        default=None,
        max_length=4096,
        description="Путь к пользовательскому Python-скрипту с функцией run(page, log=None)",
    )
    device_preset: str | None = Field(
        None,
        max_length=128,
        description=(
            "Пресет Playwright на этот запуск (напр. iPhone 12 Pro, Pixel 7). "
            "Включает is_mobile/has_touch/viewport/UA. Перекрывает device_preset профиля; "
            "null/пусто — брать из профиля."
        ),
    )

    @field_validator("cdp_public_host")
    @classmethod
    def _normalize_cdp_public_host(cls, v: str | None) -> str | None:
        return normalize_cdp_public_host(v)


class LaunchProfileAccepted(BaseModel):
    """Ответ сразу после принятия запуска (браузер поднимается в фоне)."""

    session_id: str = Field(..., description="Идентификатор сессии для GET /sessions/{session_id}")
    profile_id: str = Field(..., description="Идентификатор профиля")
    headless: bool = Field(..., description="Режим headless, как в запросе")
    cdp_debug_port: int | None = Field(
        None,
        description="Локальный порт Chromium remote debugging; None если expose_cdp=false",
    )
    note: str = Field(
        ...,
        description="Подсказка: опрашивать сессию, пока не появится cdp_ws_url",
    )


class BrowserSessionOut(BaseModel):
    """Активная или завершённая сессия браузера (API или окно UI)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., description="ID сессии (API — hex; из UI — префикс ui-)")
    profile_id: str = Field(..., description="Какой профиль запущен")
    source: Literal["api", "ui"] = Field(
        ...,
        description="api — запущено через POST /profiles/.../launch; ui — из окна приложения",
    )
    headless: bool = Field(..., description="Headless-режим")
    start_url: str = Field(..., description="Стартовый URL запуска")
    script_path: str | None = Field(None, description="Путь к пользовательскому скрипту, если был")
    cdp_debug_port: int | None = Field(None, description="Порт remote debugging, если включён CDP")
    cdp_ws_url: str | None = Field(
        None,
        description="WebSocket CDP уровня браузера (Playwright connect_over_cdp, Puppeteer и т.д.)",
    )
    cdp_http: str | None = Field(None, description="HTTP root отладчика, напр. http://127.0.0.1:PORT")
    running: bool = Field(..., description="True, пока контекст браузера ещё жив")
    result_ok: bool | None = Field(None, description="Итог после завершения; None пока сессия не закрыта")
    result_message: str | None = Field(None, description="Текст результата или ошибки")
    log_tail: list[str] = Field(
        default_factory=list,
        description="Последние строки лога run_profile (до ~200; у UI-сессий может быть пусто в начале)",
    )


class HealthOut(BaseModel):
    status: Literal["ok"] = Field("ok", description="Сервис отвечает")


class SimpleStatusOut(BaseModel):
    """Короткий ответ об успешной операции."""

    status: str = Field(..., description="Код результата, напр. stop_requested | removed")


class RootLinksOut(BaseModel):
    """Корневой ответ со ссылками на основные разделы."""

    docs: str = Field("/docs", description="Swagger UI (интерактивная документация)")
    health: str = Field("/health", description="Проверка живости")
    profiles: str = Field("/profiles", description="Список профилей")


def _is_profile_running(profile_id: str) -> bool:
    pid = (profile_id or "").strip()
    if not pid:
        return False
    if is_profile_running_via_api(pid):
        return True
    if is_profile_running_in_ui(pid):
        return True
    return _ui_tracked_session_active(pid)


def _profile_to_out(p: BrowserProfile) -> ProfileOut:
    d = asdict(p)
    d["running"] = _is_profile_running(p.profile_id)
    return ProfileOut.model_validate(d)


def _newest_health_in_group(
    members: list[BrowserProfile],
) -> tuple[bool | None, str | None, str | None]:
    with_ts = [m for m in members if m.proxy_health_checked_at]
    if not with_ts:
        return None, None, None
    best = max(with_ts, key=lambda m: (m.proxy_health_checked_at or ""))
    return best.proxy_health_ok, best.proxy_health_checked_at, best.proxy_health_message


def _group_profiles_by_proxy(
    profiles: list[BrowserProfile] | None = None,
) -> dict[tuple[str, str | None, str | None], list[BrowserProfile]]:
    from playwright_runner import canonical_proxy_key

    groups: dict[tuple[str, str | None, str | None], list[BrowserProfile]] = {}
    for p in profiles if profiles is not None else load_profiles():
        key = canonical_proxy_key(p.proxy_server, p.proxy_username, p.proxy_password)
        if not key:
            continue
        bucket = groups.setdefault(key, [])
        if any(m.profile_id == p.profile_id for m in bucket):
            continue
        bucket.append(p)
    return groups


def _proxy_group_to_out(
    key: tuple[str, str | None, str | None],
    members: list[BrowserProfile],
) -> ProxyGroupOut:
    srv, user, password = key
    ok, ts, msg = _newest_health_in_group(members)
    return ProxyGroupOut(
        proxy_server=srv,
        proxy_username=user,
        proxy_password=password,
        profile_count=len(members),
        profiles=[ProxyProfileRef(profile_id=m.profile_id, name=m.name) for m in members],
        health_ok=ok,
        health_checked_at=ts,
        health_message=msg,
    )


def _list_proxy_groups_out() -> list[ProxyGroupOut]:
    groups = _group_profiles_by_proxy()
    rows = [_proxy_group_to_out(k, m) for k, m in groups.items()]
    rows.sort(
        key=lambda g: (
            0 if g.health_ok is True else 1 if g.health_ok is False else 2,
            (g.proxy_server or "").lower(),
            (g.proxy_username or "").lower(),
        )
    )
    return rows


def _session_dict_to_out(d: dict[str, Any]) -> BrowserSessionOut:
    return BrowserSessionOut.model_validate(d)


def _pick_free_loopback_port_once() -> int:
    """Bind to an ephemeral loopback port, then release it (port may be reused immediately by OS)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _reserved_cdp_ports_locked() -> set[int]:
    """CDP ports already handed out to active sessions (caller must hold _lock)."""
    used: set[int] = set()
    for s in _sessions.values():
        if not s.finished and s.cdp_debug_port is not None:
            used.add(int(s.cdp_debug_port))
        if not s.finished and s.cdp_chrome_port is not None:
            used.add(int(s.cdp_chrome_port))
    for u in _ui_sessions.values():
        if u.cdp_debug_port is not None:
            used.add(int(u.cdp_debug_port))
    return used


def _start_cdp_socat_proxy(*, listen_port: int, chrome_port: int) -> subprocess.Popen[bytes]:
    """Chromium binds CDP to 127.0.0.1 only; socat exposes listen_port on 0.0.0.0."""
    socat = shutil.which("socat")
    if not socat:
        raise RuntimeError("socat not found (install: apt install socat)")
    cmd = [
        socat,
        f"TCP-LISTEN:{int(listen_port)},bind=0.0.0.0,fork,reuseaddr",
        f"TCP:127.0.0.1:{int(chrome_port)}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.15)
    if proc.poll() is not None:
        raise RuntimeError(f"socat exited immediately (listen {listen_port} -> 127.0.0.1:{chrome_port})")
    return proc


def _stop_cdp_socat_proxy(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _pick_free_loopback_port_avoiding(used: set[int]) -> int:
    """Pick a port not already assigned to another session (avoids duplicate ephemeral picks)."""
    return _pick_free_tcp_port_avoiding(used, bind_host="127.0.0.1")


def _pick_free_tcp_port_avoiding(used: set[int], *, bind_host: str) -> int:
    avoided = set(used)
    host = (bind_host or "127.0.0.1").strip() or "127.0.0.1"
    for _ in range(256):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            p = int(s.getsockname()[1])
        if p not in avoided:
            return p
        avoided.add(p)
    raise RuntimeError("Could not allocate a unique CDP debug port")


def _is_tcp_port_available(port: int, bind_host: str) -> bool:
    host = (bind_host or "127.0.0.1").strip() or "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, int(port)))
            return True
        except OSError:
            return False


def _cdp_bind_host(cdp_bind: str) -> str:
    return "0.0.0.0" if (cdp_bind or "").strip().lower() == "all" else "127.0.0.1"


def _resolve_cdp_public_host(body: LaunchProfileBody) -> str | None:
    raw = body.cdp_public_host or (os.environ.get("ANTIDETECT_CDP_PUBLIC_HOST") or "").strip() or None
    return normalize_cdp_public_host(raw)


def _allocate_cdp_port(
    *,
    requested: int | None,
    used: set[int],
    bind_host: str,
) -> int:
    if requested is not None:
        port = int(requested)
        if port in used:
            raise HTTPException(
                status_code=409,
                detail=f"CDP port {port} is already assigned to another active session",
            )
        if not _is_tcp_port_available(port, bind_host):
            raise HTTPException(
                status_code=409,
                detail=f"CDP port {port} is not available on {bind_host}",
            )
        return port
    return _pick_free_tcp_port_avoiding(used, bind_host=bind_host)


# RLock: to_public_dict() takes the same lock as list_sessions() while iterating — plain Lock deadlocks.
_lock = threading.RLock()
_sessions: dict[str, "ProfileRunSession"] = {}
_profile_busy: dict[str, str] = {}  # profile_id -> session_id (API)

# Запуски из окна Qt: те же GET /sessions, POST /sessions/{id}/stop
_ui_sessions: dict[str, "UiRunSession"] = {}
_ui_profile_busy: dict[str, str] = {}  # profile_id -> session_id только пока running

_hooks_lock = threading.Lock()
_log_hook: Callable[[str], None] | None = None
_sync_hook: Callable[[str], None] | None = None  # profile_id -> refresh Run button in UI
_sync_metadata_hook: Callable[[str], None] | None = None  # profile_id -> reload profiles from disk in UI

# Обновляется только из GUI-потока; читается из потоков API без обращения к QThread.
_ui_running_lock = threading.Lock()
_ui_running_profile_ids: set[str] = set()


def set_ui_profile_running(profile_id: str, running: bool) -> None:
    """Вызывается из GUI при старте/завершении RunnerThread."""
    pid = (profile_id or "").strip()
    if not pid:
        return
    with _ui_running_lock:
        if running:
            _ui_running_profile_ids.add(pid)
        else:
            _ui_running_profile_ids.discard(pid)


def is_profile_running_in_ui(profile_id: str) -> bool:
    """Потокобезопасная проверка без QThread.isRunning() из чужого потока."""
    pid = (profile_id or "").strip()
    if not pid:
        return False
    with _ui_running_lock:
        return pid in _ui_running_profile_ids


def set_api_ui_hooks(
    *,
    log_line: Callable[[str], None] | None = None,
    sync_profile_button: Callable[[str], None] | None = None,
    sync_profile_metadata: Callable[[str], None] | None = None,
) -> None:
    """Called from the Qt main thread after MainWindow is ready (optional hooks)."""
    global _log_hook, _sync_hook, _sync_metadata_hook
    with _hooks_lock:
        _log_hook = log_line
        _sync_hook = sync_profile_button
        _sync_metadata_hook = sync_profile_metadata
    set_profiles_ui_log_hook(log_line)
    if log_line:
        try:
            ud = chromium_user_data_parent()
            try:
                resolved_ud = str(ud.resolve())
            except OSError:
                resolved_ud = str(ud)
            log_line(f"Каталог данных профилей Chromium (user-data): {resolved_ud}")
        except Exception:
            pass


def _ui_log(msg: str) -> None:
    with _hooks_lock:
        fn = _log_hook
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


def _ui_sync_profile(profile_id: str) -> None:
    with _hooks_lock:
        fn = _sync_hook
    if fn:
        try:
            fn(profile_id)
        except Exception:
            pass


def _ui_sync_profile_metadata(profile_id: str) -> None:
    with _hooks_lock:
        fn = _sync_metadata_hook
    if fn:
        try:
            fn(profile_id)
        except Exception:
            pass


def is_profile_running_via_api(profile_id: str) -> bool:
    with _lock:
        return profile_id in _profile_busy


def _ui_tracked_session_active(profile_id: str) -> bool:
    """Профиль запущен из UI и ещё не завершён (есть в /sessions как running)."""
    with _lock:
        sid = _ui_profile_busy.get(profile_id)
        if not sid:
            return False
        u = _ui_sessions.get(sid)
        return bool(u and not u.finished)


def request_stop_by_profile_id(profile_id: str, *, from_ui: bool = False) -> bool:
    """Остановка по profile_id: сессия API (stop_event) или UI (колбэк из register_ui_session).

    from_ui=True: после закрытия браузера запись API-сессии удаляется из GET /sessions (кнопка в UI).
    """
    with _lock:
        sid = _profile_busy.get(profile_id)
        if sid:
            sess = _sessions.get(sid)
            if sess:
                sess.stop_event.set()
                if from_ui:
                    sess.drop_after_close = True
                return True
        sid_ui = _ui_profile_busy.get(profile_id)
        ui = _ui_sessions.get(sid_ui) if sid_ui else None
        cb = ui._stop_cb if ui and not ui.finished else None
    if cb:
        try:
            cb()
        except Exception:
            pass
        return True
    return False


@dataclass
class UiRunSession:
    session_id: str
    profile_id: str
    headless: bool
    _stop_cb: Callable[[], None] = field(repr=False)
    start_url: str = "https://studio.youtube.com"
    script_path: str | None = None
    cdp_debug_port: int | None = None
    cdp_ws_url: str | None = None
    cdp_http: str | None = None
    log_lines: list[str] = field(default_factory=list)
    finished: bool = False
    result_ok: bool | None = None
    result_message: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        with _lock:
            tail = self.log_lines[-200:] if self.log_lines else []
            return {
                "session_id": self.session_id,
                "profile_id": self.profile_id,
                "source": "ui",
                "headless": self.headless,
                "start_url": self.start_url,
                "script_path": self.script_path,
                "cdp_debug_port": self.cdp_debug_port,
                "cdp_ws_url": self.cdp_ws_url,
                "cdp_http": self.cdp_http,
                "running": not self.finished,
                "result_ok": self.result_ok,
                "result_message": self.result_message,
                "log_tail": tail,
            }


def append_ui_session_log(session_id: str, line: str) -> None:
    """Строка лога run_profile для UI-сессии (вызывается из RunnerThread)."""
    if not session_id.startswith("ui-"):
        return
    raw = line.rstrip("\n")
    with _lock:
        u = _ui_sessions.get(session_id)
        if not u:
            return
        u.log_lines.append(raw)
        if len(u.log_lines) > 4000:
            u.log_lines = u.log_lines[-2500:]


def apply_ui_session_cdp(session_id: str, info: dict[str, object]) -> None:
    if not session_id.startswith("ui-"):
        return
    with _lock:
        u = _ui_sessions.get(session_id)
        if not u:
            return
        ws = info.get("webSocketDebuggerUrl")
        if isinstance(ws, str):
            u.cdp_ws_url = ws
        http = info.get("http_debugger")
        if isinstance(http, str):
            u.cdp_http = http
        pid = u.profile_id
    ws_s = info.get("webSocketDebuggerUrl")
    if isinstance(ws_s, str) and ws_s.strip():
        short = ws_s.strip()
        if len(short) > 120:
            short = short[:117] + "..."
        _ui_log(f"[UI:{pid}] CDP: {short}")
    _ui_sync_profile(pid)


def register_ui_session(
    profile_id: str,
    stop_cb: Callable[[], None],
    *,
    headless: bool = False,
    start_url: str = "https://studio.youtube.com",
    script_path: str | None = None,
    expose_cdp: bool = True,
) -> tuple[str, int | None]:
    """Вызывается из GUI при старте RunnerThread. Возвращает (session_id, cdp_debug_port | None)."""
    sid = "ui-" + uuid.uuid4().hex[:14]
    su = (start_url or "https://studio.youtube.com").strip() or "https://studio.youtube.com"
    sp = (script_path or "").strip() or None
    with _lock:
        if profile_id in _ui_profile_busy:
            sid0 = _ui_profile_busy[profile_id]
            u0 = _ui_sessions.get(sid0)
            return sid0, (u0.cdp_debug_port if u0 else None)
        denied = _reserved_cdp_ports_locked()
        cdp_port: int | None = _pick_free_loopback_port_avoiding(denied) if expose_cdp else None
        sess = UiRunSession(
            session_id=sid,
            profile_id=profile_id,
            headless=headless,
            _stop_cb=stop_cb,
            start_url=su,
            script_path=sp,
            cdp_debug_port=cdp_port,
        )
        _ui_sessions[sid] = sess
        _ui_profile_busy[profile_id] = sid
    _ui_sync_profile(profile_id)
    return sid, cdp_port


def notify_ui_session_finished(session_id: str, ok: bool, message: str) -> None:
    """Вызывается из GUI, когда RunnerThread завершился — запись сразу убирается из GET /sessions."""
    with _lock:
        u = _ui_sessions.pop(session_id, None)
        if not u:
            return
        pid = u.profile_id
        _ui_profile_busy.pop(pid, None)
    msg = (message or "").strip() or "—"
    _ui_log(f"[UI:{pid}] сессия {session_id} завершена: {'OK' if ok else 'FAIL'} — {msg}")
    _ui_sync_profile(pid)


@dataclass
class ProfileRunSession:
    session_id: str
    profile_id: str
    headless: bool
    cdp_debug_port: int | None
    cdp_chrome_port: int | None = None
    cdp_debug_bind: str = "127.0.0.1"
    cdp_public_host: str | None = None
    cdp_socat_proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    start_url: str = "https://studio.youtube.com"
    script_path: str | None = None
    cdp_ws_url: str | None = None
    cdp_http: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    log_lines: list[str] = field(default_factory=list)
    finished: bool = False
    result_ok: bool | None = None
    result_message: str = ""
    drop_after_close: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        with _lock:
            tail = self.log_lines[-200:] if self.log_lines else []
            return {
                "session_id": self.session_id,
                "profile_id": self.profile_id,
                "source": "api",
                "headless": self.headless,
                "start_url": self.start_url,
                "script_path": self.script_path,
                "cdp_debug_port": self.cdp_debug_port,
                "cdp_ws_url": self.cdp_ws_url,
                "cdp_http": self.cdp_http,
                "running": not self.finished,
                "result_ok": self.result_ok,
                "result_message": self.result_message,
                "log_tail": tail,
            }


def _find_profile(profile_id: str) -> BrowserProfile | None:
    return get_profile(profile_id)


def _require_non_empty_tag(tag: str) -> str:
    t = (tag or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="Tag must be non-empty")
    if len(t) > 64:
        raise HTTPException(status_code=400, detail="Tag is too long (max 64)")
    return t


def _require_custom_data_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        raise HTTPException(status_code=400, detail="Key must be non-empty")
    if len(k) > 256:
        raise HTTPException(status_code=400, detail="Key is too long (max 256)")
    return k


def _mutate_profile_custom_data(
    profile_id: str,
    *,
    replace: dict[str, Any] | None = None,
    merge: dict[str, Any] | None = None,
    set_key: tuple[str, Any] | None = None,
    delete_key: str | None = None,
) -> ProfileOut:
    pid = (profile_id or "").strip()
    if not pid:
        raise HTTPException(status_code=404, detail="Profile not found")
    p = get_profile(pid)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    current = dict(p.custom_data or {})
    if replace is not None:
        current = normalize_custom_data(replace)
    if merge is not None:
        current = normalize_custom_data({**current, **merge})
    if set_key is not None:
        k, v = set_key
        kk = _require_custom_data_key(k)
        trial = normalize_custom_data({kk: v})
        if kk not in trial:
            raise HTTPException(status_code=400, detail="Value is not JSON-serializable")
        current = {**current, kk: trial[kk]}
    if delete_key is not None:
        dk = _require_custom_data_key(delete_key)
        current = {k: v for k, v in current.items() if k != dk}
    updated = update_profile_custom_data(pid, current)
    if not updated:
        raise HTTPException(status_code=404, detail="Profile not found")
    _ui_sync_profile_metadata(pid)
    return _profile_to_out(updated)


def _session_worker(sess: ProfileRunSession, profile: BrowserProfile, body: LaunchProfileBody) -> None:
    prefix = f"[API:{profile.name}:{profile.profile_id}]"

    def log(line: str) -> None:
        with _lock:
            sess.log_lines.append(line.rstrip("\n"))
            if len(sess.log_lines) > 4000:
                sess.log_lines = sess.log_lines[-2500:]
        # Строки доступны в GET /sessions/{id} → log_tail; не заливаем GUI на каждую строку.

    def on_cdp(info: dict[str, object]) -> None:
        chrome_port = sess.cdp_chrome_port if sess.cdp_chrome_port is not None else sess.cdp_debug_port
        ws_raw = info.get("webSocketDebuggerUrl")
        http_raw = info.get("http_debugger")
        ws_out = ws_raw if isinstance(ws_raw, str) else None
        http_out = http_raw if isinstance(http_raw, str) else None

        if (
            sess.cdp_debug_bind == "0.0.0.0"
            and sess.cdp_debug_port is not None
            and chrome_port is not None
            and sess.cdp_debug_port != chrome_port
        ):
            if sess.cdp_socat_proc is None:
                try:
                    sess.cdp_socat_proc = _start_cdp_socat_proxy(
                        listen_port=sess.cdp_debug_port,
                        chrome_port=chrome_port,
                    )
                    log(
                        f"CDP proxy: 0.0.0.0:{sess.cdp_debug_port} -> 127.0.0.1:{chrome_port} "
                        f"(Chromium CDP is loopback-only)"
                    )
                except Exception as e:
                    log(f"CDP proxy (socat) failed: {e}")
            if isinstance(ws_raw, str) and ws_raw.strip():
                ws_out, http_out = rewrite_cdp_public_urls(
                    ws_raw.strip(),
                    debug_port=int(chrome_port),
                    public_host=sess.cdp_public_host,
                    public_port=int(sess.cdp_debug_port),
                )

        with _lock:
            if ws_out:
                sess.cdp_ws_url = ws_out
            if http_out:
                sess.cdp_http = http_out
        if ws_out:
            short = ws_out.strip()
            if len(short) > 120:
                short = short[:117] + "..."
            _ui_log(f"{prefix} CDP: {short}")
        _ui_sync_profile(sess.profile_id)

    try:
        chrome_port = sess.cdp_chrome_port if sess.cdp_chrome_port is not None else sess.cdp_debug_port
        expose_port = (
            sess.cdp_debug_port
            if sess.cdp_debug_bind == "0.0.0.0" and sess.cdp_debug_port is not None
            else None
        )

        run_profile_obj = profile
        launch_device = (body.device_preset or "").strip() or None
        if launch_device:
            run_profile_obj = replace(profile, device_preset=launch_device)
            log(f"Launch device_preset override: {launch_device!r}")

        res = run_profile(
            run_profile_obj,
            start_url=(body.start_url or "https://studio.youtube.com").strip() or "https://studio.youtube.com",
            script_path=(body.script_path or "").strip() or None,
            log=log,
            stop_requested=sess.stop_event.is_set,
            headless=bool(body.headless),
            cdp_debug_port=chrome_port,
            cdp_debug_bind="127.0.0.1",
            cdp_public_host=sess.cdp_public_host,
            cdp_public_port=expose_port,
            on_cdp_ready=on_cdp if chrome_port is not None else None,
        )
        with _lock:
            sess.result_ok = res.ok
            sess.result_message = res.message
    except Exception as e:
        with _lock:
            sess.result_ok = False
            sess.result_message = str(e)
    finally:
        _stop_cdp_socat_proxy(sess.cdp_socat_proc)
        sess.cdp_socat_proc = None
        with _lock:
            sess.finished = True
            _profile_busy.pop(sess.profile_id, None)
            if sess.drop_after_close:
                _sessions.pop(sess.session_id, None)
        ok = bool(sess.result_ok) if sess.result_ok is not None else False
        msg = (sess.result_message or "").strip() or "—"
        _ui_log(f"{prefix} сессия {sess.session_id} завершена: {'OK' if ok else 'FAIL'} — {msg}")
        _ui_sync_profile(sess.profile_id)


def build_app() -> FastAPI:
    token_on = bool(get_configured_token())
    if token_on:
        # Ensure private users store exists (bootstrap admin on first serve).
        get_users_store()

    app = FastAPI(
        title="Antidetect — API профилей и сессий",
        version="1.0",
        description="""
## Назначение
HTTP API для списка профилей, импорта/экспорта, запуска Chromium (Playwright), получения **CDP** и остановки сессий.
Веб-UI раздаётся с того же порта (если собран `web_dist`).

## Авторизация
В режиме `serve` (задан `ANTIDETECT_API_TOKEN`, по умолчанию `secret`):
- логин/пароль → `POST /auth/login` (сессионный Bearer);
- тот же Bearer, что у Zaliver web (файл сессий Zaliver);
- машинный токен `ANTIDETECT_API_TOKEN` / `secret` (автоматизация).

Профили и прокси изолированы по пользователю (`users/<login>/`).
Десктоп Qt без токена — API открыт, общее хранилище как раньше.

## Типичный сценарий (запуск по API)
1. **`POST /profiles/{profile_id}/launch`** — в теле: `headless`, `expose_cdp`, `cdp_port`, `cdp_bind`, `cdp_public_host`, `start_url`.
2. **`GET /sessions/{session_id}`** — повторять, пока при `expose_cdp: true` не появится **`cdp_ws_url`**.
3. Подключение: Playwright `chromium.connect_over_cdp(cdp_ws_url)` или другой CDP-клиент.
4. **`POST /sessions/{session_id}/stop`** — запросить закрытие; после завершения запись может остаться (`finished`, `running: false`) — удалить **`DELETE /sessions/{session_id}`** при необходимости.

## Ошибки
- **401** — нет или неверный Bearer-токен (только если токен настроен).
- **404** — нет профиля / сессии.
- **409** — профиль уже занят (другая сессия или запуск из UI).
- **400** — неверное состояние (например, DELETE пока сессия ещё `running`).
        """.strip(),
        openapi_tags=[
            {"name": "Сервис", "description": "Проверка доступности и ссылки на документацию."},
            {"name": "Авторизация", "description": "Логин / сессии / пользователи."},
            {"name": "Профили", "description": "Чтение, импорт/экспорт и запуск сохранённых профилей."},
            {"name": "Прокси", "description": "Группы прокси, проверка здоровья и импорт из файла."},
            {"name": "Сессии", "description": "Список активных сессий, CDP, остановка и очистка записей."},
        ],
    )

    @app.get("/health", response_model=HealthOut, tags=["Сервис"])
    def health() -> HealthOut:
        return HealthOut()

    # Login is public; other auth routes use require_api_token themselves.
    app.include_router(build_auth_router())

    api_deps = [Depends(require_api_token)] if token_on else []
    api = APIRouter(dependencies=api_deps)

    if token_on:
        from auth_runtime import multiuser_enabled
        from api_auth import _resolve_bearer

        @app.middleware("http")
        async def _bind_multiuser_data_root(request: Request, call_next):
            """
            Set ContextVar in the async request context so sync route handlers
            (threadpool) inherit the per-user data root via anyio context copy.
            """
            if multiuser_enabled():
                name = None
                auth = request.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    raw = auth.split(" ", 1)[1].strip()
                    if raw:
                        try:
                            ctx = _resolve_bearer(raw)
                            name = ctx.user.username
                        except HTTPException:
                            name = None
                # Prefer ContextVar (propagates to worker threads).
                user_data_scope.set(name.lower() if name else None)
                set_data_username(name)
            try:
                return await call_next(request)
            finally:
                user_data_scope.set(None)
                set_data_username(None)

    @api.get("/profiles", response_model=List[ProfileOut], tags=["Профили"])
    def list_profiles() -> list[ProfileOut]:
        return [_profile_to_out(p) for p in load_profiles()]

    @api.post(
        "/profiles/cookie-hosts",
        response_model=CookieHostsOut,
        tags=["Профили"],
        summary="Список доменов cookies для экспорта",
    )
    def list_cookie_hosts_for_export(body: CookieHostsBody) -> CookieHostsOut:
        from cookies_io import collect_hosts_for_profiles

        ids = [str(x).strip() for x in (body.profile_ids or []) if str(x).strip()]
        if not ids:
            ids = [p.profile_id for p in load_profiles()]
        rows = collect_hosts_for_profiles(ids)
        return CookieHostsOut(hosts=[CookieHostOut(host=h, count=c) for h, c in rows])

    @api.post(
        "/profiles/export",
        tags=["Профили"],
        summary="Экспорт профилей в ZIP",
        response_class=FileResponse,
    )
    def export_profiles(body: ExportProfilesBody) -> FileResponse:
        from profiles_bundle import export_profiles_cookies_zip, export_profiles_zip

        all_profiles = load_profiles()
        by_id = {p.profile_id: p for p in all_profiles}
        wanted = [str(x).strip() for x in (body.profile_ids or []) if str(x).strip()]
        if wanted:
            missing = [pid for pid in wanted if pid not in by_id]
            if missing:
                raise HTTPException(
                    status_code=404,
                    detail=f"Profiles not found: {', '.join(missing[:8])}",
                )
            selected = [by_id[pid] for pid in wanted]
        else:
            selected = list(all_profiles)
        if not selected:
            raise HTTPException(status_code=400, detail="No profiles to export")

        tmp_dir = Path(tempfile.mkdtemp(prefix="antidetect_export_"))
        try:
            if body.mode == "cookies":
                hosts = {str(h).strip() for h in (body.hosts or []) if str(h).strip()}
                if not hosts:
                    raise HTTPException(
                        status_code=400,
                        detail="hosts must be non-empty for mode=cookies",
                    )
                out_path = export_profiles_cookies_zip(tmp_dir, selected, hosts)
            else:
                out_path = export_profiles_zip(tmp_dir, selected)
        except HTTPException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        except ValueError as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=str(e) or "Export failed") from e
        except Exception as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Export failed: {e}") from e

        def _cleanup() -> None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return FileResponse(
            path=str(out_path),
            filename=out_path.name,
            media_type="application/zip",
            background=BackgroundTask(_cleanup),
        )

    @api.post(
        "/profiles/import",
        response_model=ImportProfilesOut,
        tags=["Профили"],
        summary="Импорт профилей из ZIP",
    )
    async def import_profiles(file: UploadFile = File(...)) -> ImportProfilesOut:
        from profiles_bundle import import_profiles_zip

        name = (file.filename or "").strip().lower()
        if name and not name.endswith(".zip"):
            raise HTTPException(status_code=400, detail="Expected a .zip archive")

        tmp_dir = Path(tempfile.mkdtemp(prefix="antidetect_import_"))
        zip_path = tmp_dir / "upload.zip"
        try:
            raw = await file.read()
            if not raw:
                raise HTTPException(status_code=400, detail="Empty upload")
            zip_path.write_bytes(raw)
            # Sync Playwright (cookies inject) must not run on the asyncio loop thread.
            profiles, imported, remapped = await asyncio.to_thread(
                import_profiles_zip, zip_path
            )
            _ui_log(f"[API] импорт ZIP: +{imported} профилей (переназначено ID: {remapped})")
            return ImportProfilesOut(imported=imported, remapped=remapped, total=len(profiles))
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e) or "Import failed") from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Import failed: {e}") from e
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @api.post(
        "/profiles/delete",
        response_model=DeleteProfilesOut,
        tags=["Профили"],
        summary="Удалить выбранные профили",
    )
    def delete_profiles(body: DeleteProfilesBody) -> DeleteProfilesOut:
        wanted = [str(x).strip() for x in (body.profile_ids or []) if str(x).strip()]
        # Preserve order, drop duplicates
        seen: set[str] = set()
        ids: list[str] = []
        for pid in wanted:
            if pid not in seen:
                seen.add(pid)
                ids.append(pid)
        if not ids:
            raise HTTPException(status_code=400, detail="profile_ids must be non-empty")

        profiles = load_profiles()
        by_id = {p.profile_id: p for p in profiles}
        missing = [pid for pid in ids if pid not in by_id]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"Profiles not found: {', '.join(missing[:8])}",
            )

        running = [pid for pid in ids if _is_profile_running(pid)]
        if running:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot delete while browser is running. Stop first: "
                    + ", ".join(running[:8])
                ),
            )

        remove_ids = set(ids)
        if body.purge_data:
            root = chromium_user_data_parent()
            for pid in ids:
                try:
                    shutil.rmtree(root / pid, ignore_errors=True)
                except Exception:
                    pass

        remaining = [p for p in profiles if p.profile_id not in remove_ids]
        save_profiles(remaining)
        for pid in ids:
            _ui_sync_profile_metadata(pid)
        _ui_log(f"[API] удалено профилей: {len(ids)}")
        return DeleteProfilesOut(deleted=len(ids), deleted_ids=ids, total=len(remaining))

    @api.get("/proxies", response_model=List[ProxyGroupOut], tags=["Прокси"], summary="Список уникальных прокси")
    def list_proxies() -> list[ProxyGroupOut]:
        return _list_proxy_groups_out()

    @api.patch(
        "/proxies",
        response_model=ProxyUpdateOut,
        tags=["Прокси"],
        summary="Изменить прокси у группы профилей",
    )
    def update_proxy_group(body: ProxyUpdateBody) -> ProxyUpdateOut:
        from playwright_runner import canonical_proxy_key, normalize_proxy_server_url
        from proxy_import import apply_proxy_and_sync_geo

        match_srv = (body.match_proxy_server or "").strip()
        match_user = (body.match_proxy_username or "").strip() or None
        match_pass = (body.match_proxy_password or "").strip() or None
        old_key = canonical_proxy_key(match_srv, match_user, match_pass)
        if not old_key:
            raise HTTPException(status_code=400, detail="Invalid match_proxy_server")

        new_raw = (body.proxy_server or "").strip()
        if not new_raw:
            raise HTTPException(status_code=400, detail="proxy_server is required")
        new_srv = normalize_proxy_server_url(new_raw)
        new_user = (body.proxy_username or "").strip() or None
        new_pass = (body.proxy_password or "").strip() or None
        new_key = canonical_proxy_key(new_srv, new_user, new_pass)
        if not new_key:
            raise HTTPException(status_code=400, detail="Invalid proxy_server")

        profiles = load_profiles()
        groups = _group_profiles_by_proxy(profiles)
        members = groups.get(old_key)
        if not members:
            raise HTTPException(status_code=404, detail="Proxy group not found")

        if new_key == old_key:
            return ProxyUpdateOut(updated=0, group=_proxy_group_to_out(old_key, members))

        id_set = {m.profile_id for m in members}
        updated_n = 0
        new_list: list[BrowserProfile] = []
        for p in profiles:
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
                updated_n += 1
            else:
                new_list.append(p)

        save_profiles(new_list)
        _ui_log(f"[API] правка прокси: {old_key[0]} → {new_key[0]} ({updated_n} профил.)")
        fresh_groups = _group_profiles_by_proxy(new_list)
        out_members = fresh_groups.get(new_key) or []
        if not out_members:
            raise HTTPException(status_code=500, detail="Proxy updated but group missing")
        return ProxyUpdateOut(updated=updated_n, group=_proxy_group_to_out(new_key, out_members))

    @api.post(
        "/proxies/check",
        response_model=ProxyGroupOut,
        tags=["Прокси"],
        summary="Проверить один прокси",
    )
    def check_one_proxy(body: ProxyCheckBody) -> ProxyGroupOut:
        from playwright_runner import canonical_proxy_key
        from proxy_health import probe_proxy_health_triple, update_all_profiles_matching_proxy_credentials

        srv = (body.proxy_server or "").strip()
        if not srv:
            raise HTTPException(status_code=400, detail="proxy_server is required")
        user = (body.proxy_username or "").strip() or None
        password = (body.proxy_password or "").strip() or None
        key = canonical_proxy_key(srv, user, password)
        if not key:
            raise HTTPException(status_code=400, detail="Invalid proxy_server")

        ok, msg, ts = probe_proxy_health_triple(key[0], key[1], key[2])
        profiles = load_profiles()
        updated = update_all_profiles_matching_proxy_credentials(
            profiles,
            proxy_server=key[0],
            proxy_username=key[1],
            proxy_password=key[2],
            ok=ok,
            message=msg,
            checked_at=ts,
        )
        save_profiles(updated)
        groups = _group_profiles_by_proxy(updated)
        members = groups.get(key)
        if not members:
            raise HTTPException(status_code=404, detail="No profiles use this proxy")
        _ui_log(f"[API] проверка прокси {key[0]}: {'OK' if ok else 'FAIL'} — {msg}")
        return _proxy_group_to_out(key, members)

    @api.post(
        "/proxies/check-all",
        response_model=ProxyCheckAllOut,
        tags=["Прокси"],
        summary="Проверить все уникальные прокси",
    )
    def check_all_proxies() -> ProxyCheckAllOut:
        from proxy_health import probe_proxy_health_triple, update_all_profiles_matching_proxy_credentials

        profiles = load_profiles()
        groups = _group_profiles_by_proxy(profiles)
        if not groups:
            return ProxyCheckAllOut(checked=0, ok=0, fail=0, groups=[])

        ok_n = 0
        fail_n = 0
        for key in list(groups.keys()):
            srv, user, password = key
            ok, msg, ts = probe_proxy_health_triple(srv, user, password)
            if ok:
                ok_n += 1
            else:
                fail_n += 1
            profiles = update_all_profiles_matching_proxy_credentials(
                profiles,
                proxy_server=srv,
                proxy_username=user,
                proxy_password=password,
                ok=ok,
                message=msg,
                checked_at=ts,
            )
        save_profiles(profiles)
        _ui_log(f"[API] проверка всех прокси: {ok_n} OK / {fail_n} FAIL из {len(groups)}")
        return ProxyCheckAllOut(
            checked=len(groups),
            ok=ok_n,
            fail=fail_n,
            groups=_list_proxy_groups_out(),
        )

    @api.post(
        "/proxies/import",
        response_model=ProxyImportOut,
        tags=["Прокси"],
        summary="Импорт прокси из текста (host:port:user:pass)",
    )
    def import_proxies_text(body: ProxyImportBody) -> ProxyImportOut:
        from fingerprint_generator import generate_test_fingerprint
        from proxy_health import profile_with_recorded_proxy_health
        from proxy_import import apply_proxy_and_sync_geo, parse_host_port_user_pass_line, proxy_server_url

        scheme = body.proxy_scheme
        profiles = load_profiles()
        existing_ids = {p.profile_id for p in profiles}
        created: list[BrowserProfile] = []
        skipped = 0

        for line in (body.text or "").splitlines():
            parsed = parse_host_port_user_pass_line(line)
            if parsed is None:
                if (line or "").strip():
                    skipped += 1
                continue
            host, port, user, pwd = parsed
            server = proxy_server_url(host, port, scheme)
            profile_id = uuid.uuid4().hex[:12]
            while profile_id in existing_ids or any(c.profile_id == profile_id for c in created):
                profile_id = uuid.uuid4().hex[:12]
            idx = len(profiles) + len(created) + 1
            base = BrowserProfile(profile_id=profile_id, name=f"Profile {idx}")
            p = generate_test_fingerprint(base)
            p = apply_proxy_and_sync_geo(
                p,
                proxy_server=server,
                proxy_username=user,
                proxy_password=pwd,
            )
            p = profile_with_recorded_proxy_health(p)
            created.append(p)

        if not created:
            raise HTTPException(
                status_code=400,
                detail="No valid proxy lines (expected host:port:user:pass per line)",
            )

        profiles.extend(created)
        save_profiles(profiles)
        _ui_log(f"[API] импорт прокси: +{len(created)} профилей (пропущено строк: {skipped})")
        return ProxyImportOut(
            created=len(created),
            skipped=skipped,
            profiles=[_profile_to_out(p) for p in created],
        )

    @api.get("/profiles/{profile_id}", response_model=ProfileOut, tags=["Профили"])
    def get_profile_route(profile_id: str) -> ProfileOut:
        p = _find_profile(profile_id)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")
        return _profile_to_out(p)

    @api.patch(
        "/profiles/{profile_id}",
        response_model=ProfileOut,
        tags=["Профили"],
        summary="Частичное обновление профиля",
    )
    def patch_profile(profile_id: str, body: ProfileNamePatch) -> ProfileOut:
        """Частичное обновление профиля (сейчас — только имя)."""
        pid = (profile_id or "").strip()
        if not pid:
            raise HTTPException(status_code=404, detail="Profile not found")
        if not get_profile(pid):
            raise HTTPException(status_code=404, detail="Profile not found")
        updated = update_profile_name(pid, body.name)
        if not updated:
            raise HTTPException(status_code=404, detail="Profile not found")
        _ui_sync_profile_metadata(pid)
        return _profile_to_out(updated)

    @api.post(
        "/profiles/{profile_id}/tags/{tag}",
        response_model=ProfileOut,
        tags=["Профили"],
        summary="Добавить тег профилю",
    )
    def add_profile_tag(profile_id: str, tag: str) -> ProfileOut:
        """Добавляет тег (без дублей) и сохраняет в SQLite."""
        pid = (profile_id or "").strip()
        if not pid:
            raise HTTPException(status_code=404, detail="Profile not found")
        t = _require_non_empty_tag(tag)
        p = get_profile(pid)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")
        updated = update_profile_tags(pid, normalize_tags_list([*p.tags, t]))
        if not updated:
            raise HTTPException(status_code=404, detail="Profile not found")
        _ui_sync_profile_metadata(pid)
        return _profile_to_out(updated)

    @api.put(
        "/profiles/{profile_id}/custom-data",
        response_model=ProfileOut,
        tags=["Профили"],
        summary="Заменить custom_data профиля",
    )
    def replace_profile_custom_data(profile_id: str, body: CustomDataBody) -> ProfileOut:
        """Полностью заменяет словарь custom_data (пустой объект очищает поле)."""
        return _mutate_profile_custom_data(profile_id, replace=body.data)

    @api.patch(
        "/profiles/{profile_id}/custom-data",
        response_model=ProfileOut,
        tags=["Профили"],
        summary="Объединить custom_data профиля",
    )
    def merge_profile_custom_data(profile_id: str, body: CustomDataBody) -> ProfileOut:
        """Добавляет/перезаписывает ключи из тела запроса, остальные ключи сохраняются."""
        return _mutate_profile_custom_data(profile_id, merge=body.data)

    @api.put(
        "/profiles/{profile_id}/custom-data/{key}",
        response_model=ProfileOut,
        tags=["Профили"],
        summary="Установить один ключ custom_data",
    )
    def set_profile_custom_data_key(profile_id: str, key: str, body: CustomDataValueBody) -> ProfileOut:
        return _mutate_profile_custom_data(profile_id, set_key=(key, body.value))

    @api.delete(
        "/profiles/{profile_id}/custom-data/{key}",
        response_model=ProfileOut,
        tags=["Профили"],
        summary="Удалить ключ custom_data",
    )
    def delete_profile_custom_data_key(profile_id: str, key: str) -> ProfileOut:
        return _mutate_profile_custom_data(profile_id, delete_key=key)

    @api.delete(
        "/profiles/{profile_id}/tags/{tag}",
        response_model=ProfileOut,
        tags=["Профили"],
        summary="Удалить тег у профиля",
    )
    def remove_profile_tag(profile_id: str, tag: str) -> ProfileOut:
        """Удаляет тег (если есть) и сохраняет в SQLite."""
        pid = (profile_id or "").strip()
        if not pid:
            raise HTTPException(status_code=404, detail="Profile not found")
        t = _require_non_empty_tag(tag)
        p = get_profile(pid)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")
        updated = update_profile_tags(pid, [x for x in p.tags if x != t])
        if not updated:
            raise HTTPException(status_code=404, detail="Profile not found")
        _ui_sync_profile_metadata(pid)
        return _profile_to_out(updated)

    @api.post(
        "/profiles/{profile_id}/launch",
        response_model=LaunchProfileAccepted,
        tags=["Профили"],
        summary="Запустить профиль",
    )
    def launch_profile(profile_id: str, body: LaunchProfileBody) -> LaunchProfileAccepted:
        p = _find_profile(profile_id)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")

        if is_profile_running_in_ui(profile_id):
            raise HTTPException(
                status_code=409,
                detail="Profile is already running from the UI (stop it first)",
            )
        if _ui_tracked_session_active(profile_id):
            raise HTTPException(
                status_code=409,
                detail="Profile is already running from the UI (stop it first)",
            )

        with _lock:
            if profile_id in _profile_busy:
                raise HTTPException(
                    status_code=409,
                    detail=f"Profile already running in session {_profile_busy[profile_id]}",
                )
            sid = uuid.uuid4().hex[:16]
            cdp_port: int | None = None
            cdp_chrome_port: int | None = None
            cdp_bind_host = "127.0.0.1"
            cdp_public: str | None = None
            if body.expose_cdp:
                used = _reserved_cdp_ports_locked()
                if body.cdp_bind == "all":
                    cdp_public = _resolve_cdp_public_host(body)
                    if not cdp_public:
                        raise HTTPException(
                            status_code=400,
                            detail="cdp_public_host (or env ANTIDETECT_CDP_PUBLIC_HOST) is required when cdp_bind=all",
                        )
                    cdp_port = _allocate_cdp_port(
                        requested=body.cdp_port,
                        used=used,
                        bind_host="0.0.0.0",
                    )
                    cdp_chrome_port = _pick_free_tcp_port_avoiding(
                        used | {cdp_port},
                        bind_host="127.0.0.1",
                    )
                    cdp_bind_host = "0.0.0.0"
                else:
                    cdp_port = _allocate_cdp_port(
                        requested=body.cdp_port,
                        used=used,
                        bind_host="127.0.0.1",
                    )
                    cdp_chrome_port = cdp_port
            su = (body.start_url or "https://studio.youtube.com").strip() or "https://studio.youtube.com"
            sp = (body.script_path or "").strip() or None
            sess = ProfileRunSession(
                session_id=sid,
                profile_id=profile_id,
                headless=bool(body.headless),
                cdp_debug_port=cdp_port,
                cdp_chrome_port=cdp_chrome_port,
                cdp_debug_bind=cdp_bind_host,
                cdp_public_host=cdp_public,
                start_url=su,
                script_path=sp,
            )
            scope_user = user_data_scope.get()

            def _run_session(
                sess=sess,
                profile=p,
                launch_body=body,
                scoped=scope_user,
            ) -> None:
                from profiles_store import set_data_username

                set_data_username(scoped)
                try:
                    _session_worker(sess, profile, launch_body)
                finally:
                    set_data_username(None)

            th = threading.Thread(
                target=_run_session,
                name=f"profile-run-{profile_id}",
                daemon=True,
            )
            sess.thread = th
            _sessions[sid] = sess
            _profile_busy[profile_id] = sid

        th.start()
        cdp_on = cdp_port is not None
        _ui_log(
            f"[API:{p.name}:{profile_id}] запуск по API: session={sid}, headless={bool(body.headless)}, "
            f"CDP={'внешний ' + str(cdp_port) + ' (chrome 127.0.0.1:' + str(cdp_chrome_port) + ')' if cdp_on and body.cdp_bind == 'all' else ('порт ' + str(cdp_port) if cdp_on else 'выкл.')}, "
            f"url={((body.start_url or '')[:80] + '…') if len(body.start_url or '') > 80 else (body.start_url or 'https://studio.youtube.com')}"
        )
        _ui_sync_profile(profile_id)
        return LaunchProfileAccepted(
            session_id=sid,
            profile_id=profile_id,
            headless=sess.headless,
            cdp_debug_port=cdp_port,
            note="Опрашивайте GET /sessions/{session_id}, пока не появится cdp_ws_url (при expose_cdp: true). Playwright: chromium.connect_over_cdp(ws).",
        )

    @api.post(
        "/profiles/{profile_id}/stop",
        response_model=SimpleStatusOut,
        tags=["Профили"],
        summary="Остановить профиль по ID",
    )
    def stop_profile(profile_id: str) -> SimpleStatusOut:
        pid = (profile_id or "").strip()
        if not pid:
            raise HTTPException(status_code=404, detail="Profile not found")
        if not _is_profile_running(pid):
            raise HTTPException(status_code=400, detail="Profile is not running")
        ok = request_stop_by_profile_id(pid)
        if not ok:
            raise HTTPException(status_code=400, detail="Could not stop profile")
        _ui_sync_profile(pid)
        return SimpleStatusOut(status="stop_requested")

    @api.get(
        "/sessions",
        response_model=List[BrowserSessionOut],
        tags=["Сессии"],
        summary="Список сессий",
    )
    def list_sessions() -> list[BrowserSessionOut]:
        with _lock:
            api_rows = [s.to_public_dict() for s in _sessions.values()]
            ui_rows = [u.to_public_dict() for u in _ui_sessions.values()]
        return [_session_dict_to_out(x) for x in api_rows + ui_rows]

    @api.get(
        "/sessions/{session_id}",
        response_model=BrowserSessionOut,
        tags=["Сессии"],
        summary="Одна сессия",
    )
    def get_session(session_id: str) -> BrowserSessionOut:
        with _lock:
            s = _sessions.get(session_id)
            if s:
                return _session_dict_to_out(s.to_public_dict())
            u = _ui_sessions.get(session_id)
            if u:
                return _session_dict_to_out(u.to_public_dict())
        raise HTTPException(status_code=404, detail="Session not found")

    @api.post(
        "/sessions/{session_id}/stop",
        response_model=SimpleStatusOut,
        tags=["Сессии"],
        summary="Запросить остановку сессии",
    )
    def stop_session(session_id: str) -> SimpleStatusOut:
        s: ProfileRunSession | None = None
        u: UiRunSession | None = None
        cb: Callable[[], None] | None = None
        pid: str = ""
        with _lock:
            s = _sessions.get(session_id)
            if s:
                s.stop_event.set()
                pid = s.profile_id
            else:
                u = _ui_sessions.get(session_id)
                if u:
                    if u.finished:
                        raise HTTPException(status_code=400, detail="Session already finished")
                    cb = u._stop_cb
                    pid = u.profile_id
        if s:
            _ui_log(f"[API:{session_id}] POST /stop — профиль {pid}, остановка запрошена")
            _ui_sync_profile(pid)
            return SimpleStatusOut(status="stop_requested")
        if u and cb:
            try:
                cb()
            except Exception:
                pass
            _ui_log(f"[API:{session_id}] POST /stop — UI-профиль {pid}, остановка запрошена")
            _ui_sync_profile(pid)
            return SimpleStatusOut(status="stop_requested")
        raise HTTPException(status_code=404, detail="Session not found")

    @api.delete(
        "/sessions/{session_id}",
        response_model=SimpleStatusOut,
        tags=["Сессии"],
        summary="Удалить запись о завершённой сессии",
        description="Работает только для сессий с `running: false`. Не останавливает активный браузер — сначала POST /stop.",
    )
    def forget_session(session_id: str) -> SimpleStatusOut:
        """Удаляет завершённую сессию из памяти; активный браузер не останавливает."""
        with _lock:
            s = _sessions.get(session_id)
            if s:
                if not s.finished:
                    raise HTTPException(status_code=400, detail="Session still running; POST .../stop first")
                pid = s.profile_id
                _sessions.pop(session_id, None)
                _ui_log(f"[API] DELETE /sessions/{session_id} — запись удалена (профиль {pid})")
                return SimpleStatusOut(status="removed")
            u = _ui_sessions.get(session_id)
            if u:
                if not u.finished:
                    raise HTTPException(status_code=400, detail="Session still running; POST .../stop first")
                pid = u.profile_id
                _ui_sessions.pop(session_id, None)
                _ui_log(f"[API] DELETE /sessions/{session_id} — UI-запись удалена (профиль {pid})")
                return SimpleStatusOut(status="removed")
        raise HTTPException(status_code=404, detail="Session not found")

    @api.get("/api", response_model=RootLinksOut, tags=["Сервис"])
    def api_root() -> RootLinksOut:
        return RootLinksOut()

    app.include_router(api)

    web_dist = resolve_web_dist()
    if web_dist is not None:
        _mount_web_ui(app, web_dist)
    else:

        @app.get("/", response_model=RootLinksOut, tags=["Сервис"])
        def root() -> RootLinksOut:
            return RootLinksOut()

    return app


_app: FastAPI | None = None


def _mount_web_ui(app: FastAPI, dist: Path) -> None:
    """
    Раздаёт собранный SPA.

    Важно: не вешаем catch-all ``/{full_path:path}`` — он перехватывает API вроде
    ``GET /proxies`` и отдаёт index.html. Клиентский роутер не используется
    (вкладки — state в React), достаточно ``/`` + ``/assets``.
    """
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="web-assets")

    index = dist / "index.html"

    @app.get("/")
    def web_index() -> FileResponse:
        return FileResponse(index)


def start_profile_api_background() -> str | None:
    """
    Binds local HTTP API (FastAPI + uvicorn) in a daemon thread.
    Host/port: ANTIDETECT_API_HOST (default 127.0.0.1), ANTIDETECT_API_PORT (default 18765).
    Returns base URL (e.g. http://127.0.0.1:18765) on first start, or None if already running.
    """
    import traceback

    global _app
    if _app is not None:
        return None

    host = (os.environ.get("ANTIDETECT_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port_raw = (os.environ.get("ANTIDETECT_API_PORT") or "18765").strip() or "18765"
    try:
        port = int(port_raw)
    except ValueError:
        port = 18765

    # Desktop: не генерируем токен. Auth включается только если пользователь сам
    # задал ANTIDETECT_API_TOKEN в окружении.
    _app = build_app()

    # PyInstaller windowed (console=False): stdout/stderr are None — uvicorn/logging падают.
    if getattr(sys, "frozen", False):
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

    def _serve() -> None:
        try:
            import uvicorn

            opts: dict[str, Any] = {
                "host": host,
                "port": port,
                "log_level": "warning",
                "access_log": False,
            }
            # В onefile-EXE httptools часто не тянется; h11 — чистый Python и стабильнее в сборке.
            if getattr(sys, "frozen", False):
                opts["loop"] = "asyncio"
                opts["http"] = "h11"

            uvicorn.run(_app, **opts)
        except BaseException:
            try:
                err_path = Path(tempfile.gettempdir()) / "AntidetectUI_api_error.log"
                err_path.write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass

    t = threading.Thread(target=_serve, name="antidetect-fastapi", daemon=True)
    t.start()
    base = f"http://{host}:{port}"
    from api_auth import get_configured_token

    tok = get_configured_token()
    if tok:
        print(
            f"Antidetect local API: {base}/docs (Bearer auth ON)",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"Antidetect local API: {base}/docs (open, no token — desktop mode)",
            file=sys.stderr,
            flush=True,
        )
    return base
