from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from playwright_runner import profile_user_data_dir

UNIX_TO_NT_EPOCH_OFFSET = 11644473600

_SAMESITE_TO_STR: dict[int, str] = {
    -1: "Lax",
    0: "None",
    1: "Lax",
    2: "Strict",
}


def _cookies_db_path(profile_id: str) -> Path:
    return profile_user_data_dir(profile_id) / "Default" / "Network" / "Cookies"


def _local_state_path(profile_id: str) -> Path:
    return profile_user_data_dir(profile_id) / "Local State"


def cookies_db_available(profile_id: str) -> bool:
    return _cookies_db_path(profile_id).is_file()


def nt_expires_to_unix(expires_utc: int) -> float | None:
    if not expires_utc:
        return None
    return (expires_utc / 1_000_000) - UNIX_TO_NT_EPOCH_OFFSET


def unix_expires_to_nt(expires: float | None) -> int:
    if expires is None:
        return 0
    return int((expires + UNIX_TO_NT_EPOCH_OFFSET) * 1_000_000)


def samesite_to_str(code: int) -> str:
    return _SAMESITE_TO_STR.get(code, "Lax")


@contextmanager
def _open_cookies_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Copy DB to temp file so read works even if Chromium left a lock."""
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".cookies")
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(db_path, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            yield con
        finally:
            con.close()
    finally:
        tmp.unlink(missing_ok=True)


def list_cookie_hosts(profile_id: str) -> list[tuple[str, int]]:
    db_path = _cookies_db_path(profile_id)
    if not db_path.is_file():
        return []
    with _open_cookies_db(db_path) as con:
        rows = con.execute(
            "SELECT host_key, COUNT(*) FROM cookies GROUP BY host_key ORDER BY host_key"
        ).fetchall()
        return [(str(h), int(n)) for h, n in rows]


def collect_hosts_for_profiles(profile_ids: list[str]) -> list[tuple[str, int]]:
    totals: dict[str, int] = {}
    for pid in profile_ids:
        for host, count in list_cookie_hosts(pid):
            totals[host] = totals.get(host, 0) + count
    return sorted(totals.items(), key=lambda x: (-x[1], x[0].lower()))


def _decrypted_values_map(profile_id: str) -> dict[tuple[str, str, str], str]:
    db_path = _cookies_db_path(profile_id)
    local_state = _local_state_path(profile_id)
    if not db_path.is_file():
        return {}
    try:
        import browser_cookie3
    except ImportError as e:
        raise RuntimeError("Установите browser-cookie3: pip install browser-cookie3") from e

    key_file = str(local_state) if local_state.is_file() else None
    out: dict[tuple[str, str, str], str] = {}
    cj = browser_cookie3.chromium(cookie_file=str(db_path), key_file=key_file)
    for c in cj:
        out[(c.domain, c.path, c.name)] = c.value
    return out


def read_profile_cookies(
    profile_id: str,
    hosts: set[str] | None = None,
) -> list[dict[str, Any]]:
    db_path = _cookies_db_path(profile_id)
    if not db_path.is_file():
        return []

    values = _decrypted_values_map(profile_id)
    with _open_cookies_db(db_path) as con:
        rows = con.execute(
            """
            SELECT host_key, name, path, expires_utc, is_secure, is_httponly, samesite, value
            FROM cookies
            ORDER BY host_key, name, path
            """
        ).fetchall()

    cookies: list[dict[str, Any]] = []
    for host_key, name, path, expires_utc, is_secure, is_httponly, samesite, plain_value in rows:
        host = str(host_key)
        if hosts is not None and host not in hosts:
            continue
        key = (host, str(path), str(name))
        value = str(plain_value) if plain_value else values.get(key, "")
        item: dict[str, Any] = {
            "host": host,
            "name": str(name),
            "value": value,
            "path": str(path) or "/",
            "secure": bool(is_secure),
            "httpOnly": bool(is_httponly),
            "sameSite": samesite_to_str(int(samesite)),
        }
        exp = nt_expires_to_unix(int(expires_utc))
        if exp is not None:
            item["expires"] = exp
        cookies.append(item)
    return cookies


def cookie_to_playwright(cookie: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": cookie["name"],
        "value": cookie.get("value", ""),
        "domain": cookie["host"],
        "path": cookie.get("path") or "/",
    }
    if cookie.get("expires") is not None:
        out["expires"] = float(cookie["expires"])
    if cookie.get("secure"):
        out["secure"] = True
    if cookie.get("httpOnly"):
        out["httpOnly"] = True
    ss = cookie.get("sameSite")
    if ss in ("Strict", "Lax", "None"):
        out["sameSite"] = ss
    return out


def cookies_from_json(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("Файл cookies должен содержать JSON-массив")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "").strip()
        name = str(item.get("name") or "").strip()
        if not host or not name:
            continue
        out.append(
            {
                "host": host,
                "name": name,
                "value": str(item.get("value") or ""),
                "path": str(item.get("path") or "/"),
                "secure": bool(item.get("secure")),
                "httpOnly": bool(item.get("httpOnly")),
                "sameSite": str(item.get("sameSite") or "Lax"),
                **({"expires": float(item["expires"])} if item.get("expires") is not None else {}),
            }
        )
    return out


def write_cookies_json(path: Path, cookies: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_cookies_json(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return cookies_from_json(raw)


def export_cookies_payload(
    profile_ids: list[str],
    hosts: set[str] | None,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    payload: dict[str, list[dict[str, Any]]] = {}
    for i, pid in enumerate(profile_ids):
        if progress:
            progress(f"Чтение cookies: {i + 1} / {len(profile_ids)}…")
        payload[pid] = read_profile_cookies(pid, hosts)
    return payload
