from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class BrowserProfile:
    profile_id: str
    name: str
    proxy_server: str | None = None  # e.g. http://host:port
    proxy_username: str | None = None
    proxy_password: str | None = None

    # Playwright context config (legitimate test knobs)
    engine: str | None = "chromium"   # chromium|firefox|webkit
    device_preset: str | None = None  # e.g. "iPhone 13"
    user_agent: str | None = None
    locale: str | None = None          # e.g. en-US
    timezone_id: str | None = None     # e.g. Europe/Moscow
    viewport_width: int | None = 1280
    viewport_height: int | None = 720
    color_scheme: str | None = None    # "light"|"dark"|"no-preference"
    geo_lat: float | None = None
    geo_lon: float | None = None


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
                proxy_server=_none_if_blank(item.get("proxy_server")),
                proxy_username=_none_if_blank(item.get("proxy_username")),
                proxy_password=_none_if_blank(item.get("proxy_password")),
                engine=_none_if_blank(item.get("engine")) or "chromium",
                device_preset=_none_if_blank(item.get("device_preset")),
                user_agent=_none_if_blank(item.get("user_agent")),
                locale=_none_if_blank(item.get("locale")),
                timezone_id=_none_if_blank(item.get("timezone_id")),
                viewport_width=_int_or_none(item.get("viewport_width"), default=1280),
                viewport_height=_int_or_none(item.get("viewport_height"), default=720),
                color_scheme=_none_if_blank(item.get("color_scheme")),
                geo_lat=_float_or_none(item.get("geo_lat")),
                geo_lon=_float_or_none(item.get("geo_lon")),
            )
        )

    # drop invalid ids
    return [x for x in out if x.profile_id]


def save_profiles(profiles: list[BrowserProfile]) -> None:
    p = profiles_path()
    payload: list[dict[str, Any]] = [asdict(x) for x in profiles]
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _none_if_blank(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


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

