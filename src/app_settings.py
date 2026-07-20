"""Глобальные настройки приложения Antidetect (не привязаны к профилю)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from profiles_store import _data_dir

_SETTINGS_FILENAME = "app_settings.json"
_lock = threading.RLock()

_DEFAULTS: dict[str, Any] = {
    "anticaptcha_api_key": "",
    "anticaptcha_auto_solve": True,
}


def app_settings_path() -> Path:
    return _data_dir() / _SETTINGS_FILENAME


def load_app_settings() -> dict[str, Any]:
    path = app_settings_path()
    with _lock:
        if not path.is_file():
            return dict(_DEFAULTS)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return dict(_DEFAULTS)
        if not isinstance(raw, dict):
            return dict(_DEFAULTS)
        out = dict(_DEFAULTS)
        for k, v in raw.items():
            if k in _DEFAULTS:
                out[k] = v
        return out


def save_app_settings(data: dict[str, Any]) -> None:
    path = app_settings_path()
    with _lock:
        current = load_app_settings()
        for k, v in data.items():
            if k in _DEFAULTS:
                current[k] = v
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(current, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def get_anticaptcha_api_key() -> str:
    val = load_app_settings().get("anticaptcha_api_key", "")
    return str(val or "").strip()


def set_anticaptcha_api_key(api_key: str) -> None:
    save_app_settings({"anticaptcha_api_key": (api_key or "").strip()})


def get_anticaptcha_auto_solve() -> bool:
    val = load_app_settings().get("anticaptcha_auto_solve", True)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return True


def set_anticaptcha_auto_solve(enabled: bool) -> None:
    save_app_settings({"anticaptcha_auto_solve": bool(enabled)})
