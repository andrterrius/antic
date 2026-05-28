from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace

from playwright_runner import canonical_proxy_key, probe_proxy_connection
from profiles_store import BrowserProfile


def _utc_ts() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def probe_proxy_health_triple(
    proxy_server: str,
    proxy_username: str | None,
    proxy_password: str | None,
) -> tuple[bool, str, str]:
    """Возвращает (успех, сообщение_для_пользователя, метка_времени_UTC)."""
    ip, err = probe_proxy_connection(proxy_server, proxy_username, proxy_password)
    ts = _utc_ts()
    if ip:
        return True, f"OK, выход {ip}", ts
    return False, (err or "Нет ответа"), ts


def profile_with_recorded_proxy_health(p: BrowserProfile) -> BrowserProfile:
    """Проверка прокси и запись результата в поля профиля."""
    if not (p.proxy_server or "").strip():
        return replace(p, proxy_health_ok=None, proxy_health_checked_at=None, proxy_health_message=None)
    ok, msg, ts = probe_proxy_health_triple(p.proxy_server, p.proxy_username, p.proxy_password)
    return replace(p, proxy_health_ok=ok, proxy_health_checked_at=ts, proxy_health_message=msg)


def update_all_profiles_matching_proxy_credentials(
    profiles: list[BrowserProfile],
    *,
    proxy_server: str,
    proxy_username: str | None,
    proxy_password: str | None,
    ok: bool,
    message: str,
    checked_at: str,
) -> list[BrowserProfile]:
    """Одинаковый результат проверки для всех профилей с тем же server/user/password."""
    target = canonical_proxy_key(proxy_server, proxy_username, proxy_password)
    if not target:
        return list(profiles)
    out: list[BrowserProfile] = []
    for p in profiles:
        pk = canonical_proxy_key(p.proxy_server, p.proxy_username, p.proxy_password)
        if pk == target:
            out.append(
                replace(
                    p,
                    proxy_health_ok=ok,
                    proxy_health_checked_at=checked_at,
                    proxy_health_message=message,
                )
            )
        else:
            out.append(p)
    return out
