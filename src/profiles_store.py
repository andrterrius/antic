from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable, Iterator

DB_FILENAME = "profiles.db"
LEGACY_JSON_FILENAME = "profiles.json"
LEGACY_JSON_BACKUP_SUFFIX = ".migrated"


@dataclass
class BrowserProfile:
    profile_id: str
    name: str
    # Метаданные UI: произвольное число тегов, многострочное описание
    tags: list[str] = field(default_factory=list)
    description: str | None = None
    # Произвольные JSON-совместимые данные (ключ → значение), не влияют на Playwright
    custom_data: dict[str, Any] = field(default_factory=dict)
    automation_enabled: bool = False
    proxy_server: str | None = None  # e.g. http://host:port
    proxy_username: str | None = None
    proxy_password: str | None = None
    # Последняя проверка доступности прокси (ipify через прокси); обновляется только по явной проверке / импорту
    proxy_health_ok: bool | None = None
    proxy_health_checked_at: str | None = None  # ISO-8601 UTC, напр. 2026-05-03T12:00:00Z
    proxy_health_message: str | None = None

    # Playwright context config (legitimate test knobs)
    engine: str | None = "chromium"   # chromium|firefox|webkit
    device_preset: str | None = None  # e.g. "iPhone 13"
    user_agent: str | None = None
    locale: str | None = None          # e.g. en-US
    timezone_id: str | None = None     # e.g. Europe/Moscow
    country_code: str | None = None    # ISO-3166 alpha-2, e.g. RU, US
    # Desktop UI uses system-default sizing; keep optional for compatibility (e.g., mobile emulation).
    viewport_width: int | None = None
    viewport_height: int | None = None
    color_scheme: str | None = None    # "light"|"dark"|"no-preference"
    geo_lat: float | None = None
    geo_lon: float | None = None

    # Fingerprint-consistency overrides (best-effort)
    webgl_vendor: str | None = None
    webgl_renderer: str | None = None
    # WebGL getParameter(7938) / (35724); if unset, Chromium-like defaults apply when vendor/renderer are set
    webgl_version: str | None = None
    webgl_shading_language_version: str | None = None


_PROFILE_COLUMNS: tuple[str, ...] = (
    "profile_id",
    "name",
    "tags",
    "description",
    "custom_data",
    "automation_enabled",
    "proxy_server",
    "proxy_username",
    "proxy_password",
    "proxy_health_ok",
    "proxy_health_checked_at",
    "proxy_health_message",
    "engine",
    "device_preset",
    "user_agent",
    "locale",
    "timezone_id",
    "country_code",
    "viewport_width",
    "viewport_height",
    "color_scheme",
    "geo_lat",
    "geo_lon",
    "webgl_vendor",
    "webgl_renderer",
    "webgl_version",
    "webgl_shading_language_version",
)

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    description TEXT,
    custom_data TEXT NOT NULL DEFAULT '{{}}',
    automation_enabled INTEGER NOT NULL DEFAULT 0,
    proxy_server TEXT,
    proxy_username TEXT,
    proxy_password TEXT,
    proxy_health_ok INTEGER,
    proxy_health_checked_at TEXT,
    proxy_health_message TEXT,
    engine TEXT,
    device_preset TEXT,
    user_agent TEXT,
    locale TEXT,
    timezone_id TEXT,
    country_code TEXT,
    viewport_width INTEGER,
    viewport_height INTEGER,
    color_scheme TEXT,
    geo_lat REAL,
    geo_lon REAL,
    webgl_vendor TEXT,
    webgl_renderer TEXT,
    webgl_version TEXT,
    webgl_shading_language_version TEXT
)
"""


def app_state_root() -> Path:
    """
    Корень данных приложения: рядом лежат подкаталоги `data/` (profiles.db) и `user-data/` (Chromium).
    Windows: %APPDATA%\\AntidetectUI; macOS: ~/Library/Application Support/AntidetectUI; иначе ./data от репо.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "AntidetectUI"
        return Path(__file__).resolve().parent.parent / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AntidetectUI"
    return Path(__file__).resolve().parent.parent / "data"


def _data_dir() -> Path:
    d = app_state_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


_profiles_ui_log: Callable[[str], None] | None = None
_profiles_path_logged_resolved: str | None = None


def set_profiles_ui_log_hook(fn: Callable[[str], None] | None) -> None:
    """Проброс строки в лог UI (тот же колбэк, что и set_api_ui_hooks(log_line=…))."""
    global _profiles_ui_log, _profiles_path_logged_resolved
    _profiles_ui_log = fn
    if fn is not None:
        _profiles_path_logged_resolved = None
        profiles_path()


def sqlite_db_path() -> Path:
    return _data_dir() / DB_FILENAME


def legacy_json_path() -> Path:
    return _data_dir() / LEGACY_JSON_FILENAME


def profiles_path() -> Path:
    """Путь к основному хранилищу профилей (SQLite)."""
    p = sqlite_db_path()
    log_fn = _profiles_ui_log
    if log_fn:
        try:
            resolved = str(p.resolve())
        except OSError:
            resolved = str(p)
        global _profiles_path_logged_resolved
        if _profiles_path_logged_resolved != resolved:
            _profiles_path_logged_resolved = resolved
            try:
                log_fn(f"Хранилище профилей (SQLite): {resolved}")
            except Exception:
                pass
    return p


_store_lock = threading.RLock()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TABLE_SQL)


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


@contextmanager
def _db_connection(*, write: bool = False) -> Iterator[sqlite3.Connection]:
    db_path = sqlite_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _configure_connection(conn)
        _init_schema(conn)
        yield conn
        if write:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _bool_to_db(v: bool | None) -> int | None:
    if v is None:
        return None
    return 1 if v else 0


def _bool_from_db(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return None


def _profile_to_row(p: BrowserProfile) -> tuple[Any, ...]:
    return (
        p.profile_id,
        p.name,
        json.dumps(p.tags, ensure_ascii=False),
        p.description,
        json.dumps(p.custom_data, ensure_ascii=False),
        1 if p.automation_enabled else 0,
        p.proxy_server,
        p.proxy_username,
        p.proxy_password,
        _bool_to_db(p.proxy_health_ok),
        p.proxy_health_checked_at,
        p.proxy_health_message,
        p.engine,
        p.device_preset,
        p.user_agent,
        p.locale,
        p.timezone_id,
        p.country_code,
        p.viewport_width,
        p.viewport_height,
        p.color_scheme,
        p.geo_lat,
        p.geo_lon,
        p.webgl_vendor,
        p.webgl_renderer,
        p.webgl_version,
        p.webgl_shading_language_version,
    )


def _row_to_profile(row: sqlite3.Row) -> BrowserProfile:
    tags_raw = row["tags"]
    custom_raw = row["custom_data"]
    try:
        tags = json.loads(tags_raw) if tags_raw else []
    except (TypeError, json.JSONDecodeError):
        tags = []
    try:
        custom_data = json.loads(custom_raw) if custom_raw else {}
    except (TypeError, json.JSONDecodeError):
        custom_data = {}

    return BrowserProfile(
        profile_id=str(row["profile_id"]).strip(),
        name=str(row["name"]).strip() or "Profile",
        tags=normalize_tags_list(tags),
        description=_none_if_blank(row["description"]),
        custom_data=normalize_custom_data(custom_data),
        automation_enabled=bool(row["automation_enabled"]),
        proxy_server=_none_if_blank(row["proxy_server"]),
        proxy_username=_none_if_blank(row["proxy_username"]),
        proxy_password=_none_if_blank(row["proxy_password"]),
        proxy_health_ok=_bool_from_db(row["proxy_health_ok"]),
        proxy_health_checked_at=_none_if_blank(row["proxy_health_checked_at"]),
        proxy_health_message=_none_if_blank(row["proxy_health_message"]),
        engine=_none_if_blank(row["engine"]) or "chromium",
        device_preset=_none_if_blank(row["device_preset"]),
        user_agent=_none_if_blank(row["user_agent"]),
        locale=_none_if_blank(row["locale"]),
        timezone_id=_none_if_blank(row["timezone_id"]),
        country_code=_none_if_blank(row["country_code"]),
        viewport_width=_int_or_none(row["viewport_width"], default=None),
        viewport_height=_int_or_none(row["viewport_height"], default=None),
        color_scheme=_none_if_blank(row["color_scheme"]),
        geo_lat=_float_or_none(row["geo_lat"]),
        geo_lon=_float_or_none(row["geo_lon"]),
        webgl_vendor=_none_if_blank(row["webgl_vendor"]),
        webgl_renderer=_none_if_blank(row["webgl_renderer"]),
        webgl_version=_none_if_blank(row["webgl_version"]),
        webgl_shading_language_version=_none_if_blank(row["webgl_shading_language_version"]),
    )


def profiles_from_json_list(raw: Any) -> list[BrowserProfile]:
    """Разбор списка словарей (как в profiles.json) в модели профиля."""
    if not isinstance(raw, list):
        return []

    out: list[BrowserProfile] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            BrowserProfile(
                profile_id=str(item.get("profile_id", "")).strip(),
                name=str(item.get("name", "")).strip() or "Profile",
                tags=normalize_tags_list(item.get("tags")),
                description=_none_if_blank(item.get("description")),
                custom_data=normalize_custom_data(item.get("custom_data")),
                automation_enabled=bool(item.get("automation_enabled", False)),
                proxy_server=_none_if_blank(item.get("proxy_server")),
                proxy_username=_none_if_blank(item.get("proxy_username")),
                proxy_password=_none_if_blank(item.get("proxy_password")),
                proxy_health_ok=_bool_or_none(item.get("proxy_health_ok")),
                proxy_health_checked_at=_none_if_blank(item.get("proxy_health_checked_at")),
                proxy_health_message=_none_if_blank(item.get("proxy_health_message")),
                engine=_none_if_blank(item.get("engine")) or "chromium",
                device_preset=_none_if_blank(item.get("device_preset")),
                user_agent=_none_if_blank(item.get("user_agent")),
                locale=_none_if_blank(item.get("locale")),
                timezone_id=_none_if_blank(item.get("timezone_id")),
                country_code=_none_if_blank(item.get("country_code")),
                viewport_width=_int_or_none(item.get("viewport_width"), default=None),
                viewport_height=_int_or_none(item.get("viewport_height"), default=None),
                color_scheme=_none_if_blank(item.get("color_scheme")),
                geo_lat=_float_or_none(item.get("geo_lat")),
                geo_lon=_float_or_none(item.get("geo_lon")),
                webgl_vendor=_none_if_blank(item.get("webgl_vendor")),
                webgl_renderer=_none_if_blank(item.get("webgl_renderer")),
                webgl_version=_none_if_blank(item.get("webgl_version")),
                webgl_shading_language_version=_none_if_blank(item.get("webgl_shading_language_version")),
            )
        )

    return [x for x in out if x.profile_id]


def load_profiles_from_json_file(path: Path | None = None) -> list[BrowserProfile]:
    p = path or legacy_json_path()
    if not p.is_file():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return profiles_from_json_list(raw)


def _select_profile_row(conn: sqlite3.Connection, profile_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {', '.join(_PROFILE_COLUMNS)} FROM profiles WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()


def get_profile(profile_id: str) -> BrowserProfile | None:
    pid = (profile_id or "").strip()
    if not pid or not sqlite_db_path().exists():
        return None
    with _db_connection() as conn:
        row = _select_profile_row(conn, pid)
    if not row:
        return None
    return _row_to_profile(row)


def load_profiles() -> list[BrowserProfile]:
    if not sqlite_db_path().exists():
        return []

    with _db_connection() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_PROFILE_COLUMNS)} FROM profiles ORDER BY rowid"
        ).fetchall()
    return [_row_to_profile(row) for row in rows if str(row["profile_id"]).strip()]


def update_profile_name(profile_id: str, name: str) -> BrowserProfile | None:
    """Обновляет только поле name одного профиля (без полной перезаписи базы)."""
    pid = (profile_id or "").strip()
    if not pid:
        return None
    new_name = (name or "").strip()
    if not new_name:
        return None
    with _store_lock:
        with _db_connection(write=True) as conn:
            cur = conn.execute(
                "UPDATE profiles SET name = ? WHERE profile_id = ?",
                (new_name, pid),
            )
            if cur.rowcount == 0:
                return None
            row = _select_profile_row(conn, pid)
    return _row_to_profile(row) if row else None


def update_profile_tags(profile_id: str, tags: list[str]) -> BrowserProfile | None:
    """Обновляет только поле tags одного профиля (без полной перезаписи базы)."""
    pid = (profile_id or "").strip()
    if not pid:
        return None
    tags_json = json.dumps(normalize_tags_list(tags), ensure_ascii=False)
    with _store_lock:
        with _db_connection(write=True) as conn:
            cur = conn.execute(
                "UPDATE profiles SET tags = ? WHERE profile_id = ?",
                (tags_json, pid),
            )
            if cur.rowcount == 0:
                return None
            row = _select_profile_row(conn, pid)
    return _row_to_profile(row) if row else None


def update_profile_custom_data(profile_id: str, custom_data: dict[str, Any]) -> BrowserProfile | None:
    """Обновляет только поле custom_data одного профиля."""
    pid = (profile_id or "").strip()
    if not pid:
        return None
    data_json = json.dumps(normalize_custom_data(custom_data), ensure_ascii=False)
    with _store_lock:
        with _db_connection(write=True) as conn:
            cur = conn.execute(
                "UPDATE profiles SET custom_data = ? WHERE profile_id = ?",
                (data_json, pid),
            )
            if cur.rowcount == 0:
                return None
            row = _select_profile_row(conn, pid)
    return _row_to_profile(row) if row else None


def save_profiles(profiles: list[BrowserProfile]) -> None:
    placeholders = ", ".join("?" for _ in _PROFILE_COLUMNS)
    columns = ", ".join(_PROFILE_COLUMNS)
    insert_sql = f"INSERT OR REPLACE INTO profiles ({columns}) VALUES ({placeholders})"
    keep_ids = [p.profile_id for p in profiles if p.profile_id]

    with _store_lock:
        with _db_connection(write=True) as conn:
            if keep_ids:
                conn.executemany(insert_sql, [_profile_to_row(p) for p in profiles if p.profile_id])
                placeholders_ids = ", ".join("?" for _ in keep_ids)
                conn.execute(
                    f"DELETE FROM profiles WHERE profile_id NOT IN ({placeholders_ids})",
                    keep_ids,
                )
            else:
                conn.execute("DELETE FROM profiles")


def needs_json_migration() -> bool:
    """True, если есть старый profiles.json, но ещё нет SQLite-базы."""
    return legacy_json_path().is_file() and not sqlite_db_path().is_file()


def count_legacy_json_profiles() -> int:
    return len(load_profiles_from_json_file())


def _backup_legacy_json() -> Path:
    src = legacy_json_path()
    backup = src.with_suffix(src.suffix + LEGACY_JSON_BACKUP_SUFFIX)
    if backup.exists():
        backup.unlink()
    shutil.move(str(src), str(backup))
    return backup


def migrate_json_to_sqlite(*, json_path: Path | None = None) -> int:
    """
    Переносит профили из profiles.json в SQLite.
    После успешной записи переименовывает JSON в profiles.json.migrated.
    Возвращает число перенесённых профилей.
    """
    src = json_path or legacy_json_path()
    if not src.is_file():
        return 0

    profiles = load_profiles_from_json_file(src)
    save_profiles(profiles)

    if src == legacy_json_path():
        _backup_legacy_json()

    return len(profiles)


def normalize_custom_data(raw: Any) -> dict[str, Any]:
    """Словарь с строковыми ключами и JSON-сериализуемыми значениями."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key or len(key) > 256:
            continue
        if _is_json_serializable(v):
            out[key] = v
    return out


def custom_data_to_json_text(data: dict[str, Any] | None) -> str:
    d = normalize_custom_data(data)
    if not d:
        return ""
    return json.dumps(d, ensure_ascii=False, indent=2)


def custom_data_from_json_text(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("custom_data must be a JSON object")
    return normalize_custom_data(parsed)


def normalize_tags_list(raw: Any) -> list[str]:
    """Строки тегов без пустых и без повторов (порядок первого вхождения)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        s = str(x).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def tags_from_delimited_text(text: str) -> list[str]:
    """Разбор строки тегов: запятая, точка с запятой, вертикальная черта или перевод строки."""
    if not (text or "").strip():
        return []
    parts: list[str] = []
    buf: list[str] = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch in ",;|\n":
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return normalize_tags_list(parts)


def _none_if_blank(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _bool_or_none(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v == 1:
            return True
        if v == 0:
            return False
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _int_or_none(v: Any, *, default: int | None = None) -> int | None:
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _is_json_serializable(v: Any) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False
