from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class BrowserProfile:
    profile_id: str
    name: str
    # Метаданные UI: произвольное число тегов, многострочное описание
    tags: list[str] = field(default_factory=list)
    description: str | None = None
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


def _data_dir() -> Path:
    # Store state in %APPDATA% (Roaming) to keep project directory clean.
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) / "AntidetectUI" if appdata else (Path(__file__).resolve().parent.parent / "data")
    d = root
    d.mkdir(parents=True, exist_ok=True)
    return d


def profiles_path() -> Path:
    return _data_dir() / "profiles.json"


def load_profiles() -> list[BrowserProfile]:
    p = profiles_path()
    if not p.exists():
        return []

    raw = json.loads(p.read_text(encoding="utf-8"))
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

    # drop invalid ids
    return [x for x in out if x.profile_id]


def save_profiles(profiles: list[BrowserProfile]) -> None:
    p = profiles_path()
    payload: list[dict[str, Any]] = [asdict(x) for x in profiles]
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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

