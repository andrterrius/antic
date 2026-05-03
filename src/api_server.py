from __future__ import annotations

import os
import sys
import socket
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from collections.abc import Callable

from profiles_store import BrowserProfile, load_profiles
from playwright_runner import run_profile


# --- OpenAPI / Pydantic-схемы (документация в /docs) ---


class ProfileOut(BaseModel):
    """Сохранённый профиль браузера (настройки Playwright + отпечаток)."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(..., description="Уникальный идентификатор профиля")
    name: str = Field(..., description="Имя профиля в списке")
    automation_enabled: bool = Field(False, description="Флаг автоматизации (по смыслу приложения)")
    proxy_server: str | None = Field(None, description="Прокси, напр. http://host:port или socks5://…")
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


class LaunchProfileBody(BaseModel):
    """Тело запроса на запуск профиля по HTTP."""

    headless: bool = Field(False, description="Запуск без окна (headless Chromium)")
    expose_cdp: bool = Field(
        True,
        description="Выделить порт remote debugging и заполнить cdp_ws_url в сессии (для connect_over_cdp)",
    )
    start_url: str = Field(
        default="https://2ip.ru",
        max_length=4096,
        description="Первая открываемая страница после старта контекста",
    )
    script_path: str | None = Field(
        default=None,
        max_length=4096,
        description="Путь к пользовательскому Python-скрипту с функцией run(page, log=None)",
    )


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


def _profile_to_out(p: BrowserProfile) -> ProfileOut:
    return ProfileOut.model_validate(asdict(p))


def _session_dict_to_out(d: dict[str, Any]) -> BrowserSessionOut:
    return BrowserSessionOut.model_validate(d)


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


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
_qt_runner_busy: Callable[[str], bool] | None = None  # profile_id -> UI RunnerThread active


def set_api_ui_hooks(
    *,
    log_line: Callable[[str], None] | None = None,
    sync_profile_button: Callable[[str], None] | None = None,
    is_profile_running_in_ui: Callable[[str], bool] | None = None,
) -> None:
    """Called from the Qt main thread after MainWindow is ready (optional hooks)."""
    global _log_hook, _sync_hook, _qt_runner_busy
    with _hooks_lock:
        _log_hook = log_line
        _sync_hook = sync_profile_button
        _qt_runner_busy = is_profile_running_in_ui


def _ui_runner_blocks(profile_id: str) -> bool:
    with _hooks_lock:
        fn = _qt_runner_busy
    if not fn:
        return False
    try:
        return bool(fn(profile_id))
    except Exception:
        return False


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
    start_url: str = "https://2ip.ru"
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
    start_url: str = "https://2ip.ru",
    script_path: str | None = None,
    expose_cdp: bool = True,
) -> tuple[str, int | None]:
    """Вызывается из GUI при старте RunnerThread. Возвращает (session_id, cdp_debug_port | None)."""
    sid = "ui-" + uuid.uuid4().hex[:14]
    su = (start_url or "https://2ip.ru").strip() or "https://2ip.ru"
    sp = (script_path or "").strip() or None
    cdp_port: int | None = _pick_free_loopback_port() if expose_cdp else None
    with _lock:
        if profile_id in _ui_profile_busy:
            sid0 = _ui_profile_busy[profile_id]
            u0 = _ui_sessions.get(sid0)
            return sid0, (u0.cdp_debug_port if u0 else None)
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
    start_url: str = "https://2ip.ru"
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
    pid = (profile_id or "").strip()
    if not pid:
        return None
    for p in load_profiles():
        if p.profile_id == pid:
            return p
    return None


def _session_worker(sess: ProfileRunSession, profile: BrowserProfile, body: LaunchProfileBody) -> None:
    prefix = f"[API:{profile.name}:{profile.profile_id}]"

    def log(line: str) -> None:
        with _lock:
            sess.log_lines.append(line.rstrip("\n"))
            if len(sess.log_lines) > 4000:
                sess.log_lines = sess.log_lines[-2500:]
        _ui_log(f"{prefix} {line.rstrip()}")

    def on_cdp(info: dict[str, object]) -> None:
        with _lock:
            ws = info.get("webSocketDebuggerUrl")
            if isinstance(ws, str):
                sess.cdp_ws_url = ws
            http = info.get("http_debugger")
            if isinstance(http, str):
                sess.cdp_http = http
        ws_s = info.get("webSocketDebuggerUrl")
        if isinstance(ws_s, str) and ws_s.strip():
            short = ws_s.strip()
            if len(short) > 120:
                short = short[:117] + "..."
            _ui_log(f"{prefix} CDP: {short}")
        _ui_sync_profile(sess.profile_id)

    try:
        res = run_profile(
            profile,
            start_url=(body.start_url or "https://2ip.ru").strip() or "https://2ip.ru",
            script_path=(body.script_path or "").strip() or None,
            log=log,
            stop_requested=sess.stop_event.is_set,
            headless=bool(body.headless),
            cdp_debug_port=sess.cdp_debug_port,
            on_cdp_ready=on_cdp if sess.cdp_debug_port is not None else None,
        )
        with _lock:
            sess.result_ok = res.ok
            sess.result_message = res.message
    except Exception as e:
        with _lock:
            sess.result_ok = False
            sess.result_message = str(e)
    finally:
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
    app = FastAPI(
        title="Antidetect — API профилей и сессий",
        version="1.0",
        description="""
## Назначение
Локальный HTTP API для списка профилей, запуска Chromium (Playwright), получения **CDP** (`webSocketDebuggerUrl`) и остановки сессий.
Сессии, запущенные из окна приложения, тоже видны в `GET /sessions` (`source: ui`).

## Типичный сценарий (запуск по API)
1. **`POST /profiles/{profile_id}/launch`** — в теле можно задать `headless`, `expose_cdp`, `start_url`.
2. **`GET /sessions/{session_id}`** — повторять, пока при `expose_cdp: true` не появится **`cdp_ws_url`**.
3. Подключение: Playwright `chromium.connect_over_cdp(cdp_ws_url)` или другой CDP-клиент.
4. **`POST /sessions/{session_id}/stop`** — запросить закрытие; после завершения запись может остаться (`finished`, `running: false`) — удалить **`DELETE /sessions/{session_id}`** при необходимости.

## Ошибки
- **404** — нет профиля / сессии.
- **409** — профиль уже занят (другая сессия или запуск из UI).
- **400** — неверное состояние (например, DELETE пока сессия ещё `running`).
        """.strip(),
        openapi_tags=[
            {"name": "Сервис", "description": "Проверка доступности и ссылки на документацию."},
            {"name": "Профили", "description": "Чтение и запуск сохранённых профилей."},
            {"name": "Сессии", "description": "Список активных сессий, CDP, остановка и очистка записей."},
        ],
    )

    @app.get("/health", response_model=HealthOut, tags=["Сервис"])
    def health() -> HealthOut:
        return HealthOut()

    @app.get("/profiles", response_model=list[ProfileOut], tags=["Профили"])
    def list_profiles() -> list[ProfileOut]:
        return [_profile_to_out(p) for p in load_profiles()]

    @app.get("/profiles/{profile_id}", response_model=ProfileOut, tags=["Профили"])
    def get_profile(profile_id: str) -> ProfileOut:
        p = _find_profile(profile_id)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")
        return _profile_to_out(p)

    @app.post(
        "/profiles/{profile_id}/launch",
        response_model=LaunchProfileAccepted,
        tags=["Профили"],
        summary="Запустить профиль",
    )
    def launch_profile(profile_id: str, body: LaunchProfileBody) -> LaunchProfileAccepted:
        p = _find_profile(profile_id)
        if not p:
            raise HTTPException(status_code=404, detail="Profile not found")

        if _ui_runner_blocks(profile_id):
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
            cdp_port: int | None = _pick_free_loopback_port() if body.expose_cdp else None
            su = (body.start_url or "https://2ip.ru").strip() or "https://2ip.ru"
            sp = (body.script_path or "").strip() or None
            sess = ProfileRunSession(
                session_id=sid,
                profile_id=profile_id,
                headless=bool(body.headless),
                cdp_debug_port=cdp_port,
                start_url=su,
                script_path=sp,
            )
            th = threading.Thread(
                target=_session_worker,
                args=(sess, p, body),
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
            f"CDP={'порт ' + str(cdp_port) if cdp_on else 'выкл.'}, url={((body.start_url or '')[:80] + '…') if len(body.start_url or '') > 80 else (body.start_url or 'https://2ip.ru')}"
        )
        _ui_sync_profile(profile_id)
        return LaunchProfileAccepted(
            session_id=sid,
            profile_id=profile_id,
            headless=sess.headless,
            cdp_debug_port=cdp_port,
            note="Опрашивайте GET /sessions/{session_id}, пока не появится cdp_ws_url (при expose_cdp: true). Playwright: chromium.connect_over_cdp(ws).",
        )

    @app.get(
        "/sessions",
        response_model=list[BrowserSessionOut],
        tags=["Сессии"],
        summary="Список сессий",
    )
    def list_sessions() -> list[BrowserSessionOut]:
        with _lock:
            api_rows = [s.to_public_dict() for s in _sessions.values()]
            ui_rows = [u.to_public_dict() for u in _ui_sessions.values()]
        return [_session_dict_to_out(x) for x in api_rows + ui_rows]

    @app.get(
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

    @app.post(
        "/sessions/{session_id}/stop",
        response_model=SimpleStatusOut,
        tags=["Сессии"],
        summary="Запросить остановку сессии",
    )
    def stop_session(session_id: str) -> SimpleStatusOut:
        u: UiRunSession | None = None
        cb: Callable[[], None] | None = None
        pid: str = ""
        with _lock:
            s = _sessions.get(session_id)
            if s:
                s.stop_event.set()
                pid = s.profile_id
                _ui_log(f"[API:{session_id}] POST /stop — профиль {pid}, остановка запрошена")
                _ui_sync_profile(pid)
                return SimpleStatusOut(status="stop_requested")
            u = _ui_sessions.get(session_id)
            if u:
                if u.finished:
                    raise HTTPException(status_code=400, detail="Session already finished")
                cb = u._stop_cb
                pid = u.profile_id
        if u and cb:
            try:
                cb()
            except Exception:
                pass
            _ui_log(f"[API:{session_id}] POST /stop — UI-профиль {pid}, остановка запрошена")
            _ui_sync_profile(pid)
            return SimpleStatusOut(status="stop_requested")
        raise HTTPException(status_code=404, detail="Session not found")

    @app.delete(
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

    @app.get("/", response_model=RootLinksOut, tags=["Сервис"])
    def root() -> RootLinksOut:
        return RootLinksOut()

    return app


_app: FastAPI | None = None


def start_profile_api_background() -> str | None:
    """
    Binds local HTTP API (FastAPI + uvicorn) in a daemon thread.
    Host/port: ANTIDETECT_API_HOST (default 127.0.0.1), ANTIDETECT_API_PORT (default 18765).
    Returns base URL (e.g. http://127.0.0.1:18765) on first start, or None if already running.
    """
    global _app
    if _app is not None:
        return None

    host = (os.environ.get("ANTIDETECT_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port_raw = (os.environ.get("ANTIDETECT_API_PORT") or "18765").strip() or "18765"
    try:
        port = int(port_raw)
    except ValueError:
        port = 18765

    _app = build_app()

    def _serve() -> None:
        import uvicorn

        uvicorn.run(_app, host=host, port=port, log_level="warning")

    t = threading.Thread(target=_serve, name="antidetect-fastapi", daemon=True)
    t.start()
    base = f"http://{host}:{port}"
    print(f"Antidetect local API: {base}/docs (profiles, launch, CDP)", file=sys.stderr, flush=True)
    return base
