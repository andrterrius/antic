from __future__ import annotations

from typing import Any

TWOFA_KEY_MARKER = "_2fa"


def is_twofa_custom_key(key: str) -> bool:
    return TWOFA_KEY_MARKER in str(key or "")


def normalize_twofa_custom_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        raise ValueError("ключ custom_data не задан")
    if not is_twofa_custom_key(k):
        raise ValueError(f"ключ custom_data должен содержать «{TWOFA_KEY_MARKER}»")
    return k


def twofa_key_names(custom_data: dict[str, Any] | None) -> list[str]:
    data = custom_data or {}
    return [
        str(k)
        for k in sorted(data.keys(), key=lambda x: str(x).lower())
        if is_twofa_custom_key(str(k))
    ]


def collect_unique_twofa_keys(profiles: list[Any]) -> list[str]:
    """Уникальные ключи custom_data с «_2fa» по всем профилям."""
    keys: set[str] = set()
    for p in profiles:
        keys.update(twofa_key_names(getattr(p, "custom_data", None)))
    return sorted(keys, key=str.lower)


def twofa_entries(custom_data: dict[str, Any] | None) -> list[tuple[str, str]]:
    data = custom_data or {}
    out: list[tuple[str, str]] = []
    for k in twofa_key_names(data):
        val = data[k]
        secret = "" if val is None else str(val).strip()
        if secret:
            out.append((k, secret))
    return out


def profile_has_twofa(custom_data: dict[str, Any] | None) -> bool:
    return bool(twofa_entries(custom_data))


def secret_for_twofa_key(custom_data: dict[str, Any] | None, key: str) -> str:
    data = custom_data or {}
    if key not in data:
        return ""
    val = data[key]
    return "" if val is None else str(val).strip()


def set_twofa_in_custom_data(
    custom_data: dict[str, Any] | None,
    key: str,
    secret: str,
    *,
    old_key: str | None = None,
) -> dict[str, Any]:
    custom = dict(custom_data or {})
    new_key = normalize_twofa_custom_key(key)
    old = (old_key or "").strip() or None
    if old and old != new_key and old in custom:
        del custom[old]
    value = (secret or "").strip()
    if value:
        custom[new_key] = value
    elif new_key in custom:
        del custom[new_key]
    return custom
