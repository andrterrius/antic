from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
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

_SAMESITE_FROM_STR: dict[str, int] = {
    "None": 0,
    "Lax": 1,
    "Strict": 2,
}

# Chromium mock keychain / --password-store=basic (Playwright on macOS).
_CHROMIUM_DEFAULT_PASSWORD = b"peanuts"
_OSX_KEYCHAIN_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Chromium Safe Storage", "Chromium"),
    ("Chrome Safe Storage", "Chrome"),
)


def _cookies_db_candidates(profile_id: str) -> list[Path]:
    """Chromium cookie DB locations.

    Modern Chromium (Win/Linux) stores cookies in Default/Network/Cookies.
    macOS and older profiles keep them in Default/Cookies. If both exist, prefer
    the one that actually has a WAL sidecar — that is the live database.
    """
    default = profile_user_data_dir(profile_id) / "Default"
    return [
        default / "Network" / "Cookies",
        default / "Cookies",
    ]


def _db_has_rows(db_path: Path) -> bool:
    try:
        with _open_cookies_db(db_path) as (con, _snapshot):
            row = con.execute("SELECT 1 FROM cookies LIMIT 1").fetchone()
            return row is not None
    except Exception:
        return False


def _cookies_db_path(profile_id: str) -> Path | None:
    existing = [p for p in _cookies_db_candidates(profile_id) if p.is_file()]
    if not existing:
        return None
    if len(existing) == 1:
        return existing[0]
    with_wal = [p for p in existing if Path(str(p) + "-wal").is_file()]
    pool = with_wal or existing
    for p in pool:
        if _db_has_rows(p):
            return p
    return pool[0]


def _local_state_path(profile_id: str) -> Path:
    return profile_user_data_dir(profile_id) / "Local State"


def cookies_db_available(profile_id: str) -> bool:
    return _cookies_db_path(profile_id) is not None


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


def samesite_from_str(name: str | None) -> int:
    return _SAMESITE_FROM_STR.get(str(name or "Lax"), 1)


@contextmanager
def _open_cookies_db(db_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """Copy DB to a temp dir so read works even if Chromium left a lock.

    Chromium (especially on macOS) keeps new rows in Cookies-wal until checkpoint.
    Copying only the main file yields an empty cookies table. The yielded path is
    the checkpointed snapshot (safe to pass to browser-cookie3).
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="cookies-"))
    tmp = tmp_dir / "Cookies"
    try:
        shutil.copy2(db_path, tmp)
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_path) + suffix)
            if side.is_file():
                shutil.copy2(side, Path(str(tmp) + suffix))
        con = sqlite3.connect(str(tmp))
        try:
            con.execute("PRAGMA wal_checkpoint(FULL)")
            yield con, tmp
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def list_cookie_hosts(profile_id: str) -> list[tuple[str, int]]:
    db_path = _cookies_db_path(profile_id)
    if db_path is None:
        return []
    with _open_cookies_db(db_path) as (con, _snapshot):
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


def _osx_keychain_passwords() -> list[bytes]:
    """Passwords Chromium may have used to derive the cookie AES key.

    Playwright persistent contexts on macOS typically run with the mock keychain
    (password ``peanuts``). A normal Chromium/Chrome install uses Keychain
    ("Chromium Safe Storage" / "Chrome Safe Storage"). browser-cookie3 only tries
    one of those and raises "Unable to get key for cookie decryption".
    """
    found: list[bytes] = [_CHROMIUM_DEFAULT_PASSWORD]
    seen = {found[0]}
    for service, account in _OSX_KEYCHAIN_CANDIDATES:
        try:
            proc = subprocess.run(
                [
                    "/usr/bin/security",
                    "-q",
                    "find-generic-password",
                    "-w",
                    "-a",
                    account,
                    "-s",
                    service,
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        secret = proc.stdout.strip()
        if secret and secret not in seen:
            seen.add(secret)
            found.append(secret)
    return found


def _pbkdf2_sha1(password: bytes, iterations: int) -> bytes:
    from Cryptodome.Protocol.KDF import PBKDF2

    return PBKDF2(password, b"saltysalt", 16, iterations)


def _cookie_aes_keys() -> list[bytes]:
    if sys.platform == "darwin":
        return [_pbkdf2_sha1(p, 1003) for p in _osx_keychain_passwords()]
    if sys.platform.startswith("linux"):
        keys = [_pbkdf2_sha1(_CHROMIUM_DEFAULT_PASSWORD, 1), _pbkdf2_sha1(b"", 1)]
        return keys
    return []


def _aes_cbc_decrypt(blob: bytes, key: bytes) -> str | None:
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import unpad

    try:
        decrypted = unpad(AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(blob), AES.block_size)
    except (ValueError, KeyError):
        return None
    if len(decrypted) >= 32:
        body = decrypted[32:]
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            pass
    try:
        return decrypted.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _decrypt_cookie_value(encrypted: bytes, keys: list[bytes]) -> str:
    if not encrypted:
        return ""
    prefix = encrypted[:3]
    if prefix not in (b"v10", b"v11"):
        try:
            return encrypted.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    blob = encrypted[3:]
    for key in keys:
        text = _aes_cbc_decrypt(blob, key)
        if text is not None:
            return text
    return ""


def _decrypted_values_map(profile_id: str, cookie_file: Path) -> dict[tuple[str, str, str], str]:
    if not cookie_file.is_file():
        return {}
    if sys.platform == "win32":
        return _decrypted_values_map_win(profile_id, cookie_file)
    keys = _cookie_aes_keys()
    if not keys:
        return {}
    out: dict[tuple[str, str, str], str] = {}
    con = sqlite3.connect(str(cookie_file))
    try:
        rows = con.execute(
            "SELECT host_key, path, name, encrypted_value FROM cookies"
        ).fetchall()
    finally:
        con.close()
    for host, path, name, enc in rows:
        if not isinstance(enc, (bytes, bytearray)) or not enc:
            continue
        value = _decrypt_cookie_value(bytes(enc), keys)
        if value:
            out[(str(host), str(path), str(name))] = value
    return out


def _decrypted_values_map_win(profile_id: str, cookie_file: Path) -> dict[tuple[str, str, str], str]:
    local_state = _local_state_path(profile_id)
    try:
        import browser_cookie3
    except ImportError as e:
        raise RuntimeError("Установите browser-cookie3: pip install browser-cookie3") from e

    key_file = str(local_state) if local_state.is_file() else None
    out: dict[tuple[str, str, str], str] = {}
    cj = browser_cookie3.chromium(cookie_file=str(cookie_file), key_file=key_file)
    for c in cj:
        out[(c.domain, c.path, c.name)] = c.value
    return out


def read_profile_cookies(
    profile_id: str,
    hosts: set[str] | None = None,
) -> list[dict[str, Any]]:
    db_path = _cookies_db_path(profile_id)
    if db_path is None:
        return []

    with _open_cookies_db(db_path) as (con, snapshot):
        encrypted = _cookie_is_encrypted(con)
        values = _decrypted_values_map(profile_id, snapshot) if encrypted else {}
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


def _encrypt_cookie_value(value: str) -> bytes:
    """v10 CBC, Chromium mock-keychain password ``peanuts`` (Playwright on macOS)."""
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import pad

    key = _pbkdf2_sha1(_CHROMIUM_DEFAULT_PASSWORD, 1003 if sys.platform == "darwin" else 1)
    blob = AES.new(key, AES.MODE_CBC, b" " * 16).encrypt(pad(value.encode("utf-8"), AES.block_size))
    return b"v10" + blob


_COOKIE_COLUMNS = (
    "creation_utc",
    "host_key",
    "top_frame_site_key",
    "name",
    "value",
    "encrypted_value",
    "path",
    "expires_utc",
    "is_secure",
    "is_httponly",
    "last_access_utc",
    "has_expires",
    "is_persistent",
    "priority",
    "samesite",
    "source_scheme",
    "source_port",
    "last_update_utc",
)


def _cookie_is_encrypted(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM cookies WHERE length(encrypted_value) > 0 LIMIT 1"
    ).fetchone()
    return row is not None


def _cookie_row(cookie: dict[str, Any], now_nt: int, columns: set[str]) -> dict[str, Any]:
    host = str(cookie["host"])
    expires = cookie.get("expires")
    expires_nt = unix_expires_to_nt(float(expires) if expires is not None else None)
    has_expires = 1 if expires_nt else 0
    secure = 1 if cookie.get("secure") else 0
    value = str(cookie.get("value") or "")
    encrypted = b""
    plain = value
    if sys.platform == "darwin":
        encrypted = _encrypt_cookie_value(value)
        plain = ""
    row = {
        "creation_utc": now_nt,
        "host_key": host,
        "top_frame_site_key": "",
        "name": str(cookie["name"]),
        "value": plain,
        "encrypted_value": encrypted,
        "path": str(cookie.get("path") or "/"),
        "expires_utc": expires_nt,
        "is_secure": secure,
        "is_httponly": 1 if cookie.get("httpOnly") else 0,
        "last_access_utc": now_nt,
        "has_expires": has_expires,
        "is_persistent": has_expires,
        "priority": 1,
        "samesite": samesite_from_str(cookie.get("sameSite")),
        "source_scheme": 2 if secure else 1,
        "source_port": 443 if secure else 80,
        "last_update_utc": now_nt,
    }
    return {k: v for k, v in row.items() if k in columns}


def _ensure_cookies_schema(con: sqlite3.Connection) -> set[str]:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS cookies (
            creation_utc INTEGER NOT NULL,
            host_key TEXT NOT NULL,
            top_frame_site_key TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            encrypted_value BLOB NOT NULL DEFAULT '',
            path TEXT NOT NULL,
            expires_utc INTEGER NOT NULL,
            is_secure INTEGER NOT NULL,
            is_httponly INTEGER NOT NULL,
            last_access_utc INTEGER NOT NULL,
            has_expires INTEGER NOT NULL,
            is_persistent INTEGER NOT NULL,
            priority INTEGER NOT NULL,
            samesite INTEGER NOT NULL DEFAULT -1,
            source_scheme INTEGER NOT NULL DEFAULT 0,
            source_port INTEGER NOT NULL DEFAULT -1,
            last_update_utc INTEGER NOT NULL DEFAULT 0,
            UNIQUE (host_key, top_frame_site_key, name, path)
        );
        CREATE TABLE IF NOT EXISTS meta (key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
        """
    )
    cols = {str(r[1]) for r in con.execute("PRAGMA table_info(cookies)")}
    if "top_frame_site_key" not in cols:
        con.execute("ALTER TABLE cookies ADD COLUMN top_frame_site_key TEXT NOT NULL DEFAULT ''")
    return {str(r[1]) for r in con.execute("PRAGMA table_info(cookies)")}


def write_profile_cookies(profile_id: str, cookies: list[dict[str, Any]]) -> int:
    """Insert cookies into the profile SQLite DB. Returns rows written."""
    if not cookies:
        return 0
    import time

    default = profile_user_data_dir(profile_id) / "Default"
    db_path = _cookies_db_path(profile_id)
    if db_path is None:
        db_path = default / ("Cookies" if sys.platform == "darwin" else "Network/Cookies")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.is_file():
            side.unlink()

    now_nt = unix_expires_to_nt(time.time())
    con = sqlite3.connect(str(db_path))
    try:
        columns = _ensure_cookies_schema(con)
        insert_cols = [c for c in _COOKIE_COLUMNS if c in columns]
        placeholders = ", ".join("?" for _ in insert_cols)
        sql = (
            f"INSERT OR REPLACE INTO cookies ({', '.join(insert_cols)}) "
            f"VALUES ({placeholders})"
        )
        written = 0
        for cookie in cookies:
            if not str(cookie.get("name") or "").strip():
                continue
            row = _cookie_row(cookie, now_nt, columns)
            con.execute(sql, [row[c] for c in insert_cols])
            written += 1
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('mmap_status', '-1')"
        )
        con.commit()
        return written
    finally:
        con.close()


def cookie_to_playwright(cookie: dict[str, Any]) -> dict[str, Any]:
    host = str(cookie["host"])
    secure = bool(cookie.get("secure"))
    # Playwright drops domain/path when url is also set, which turns ".site" into
    # a host-only cookie and Chromium then ignores it. Keep domain+path.
    out: dict[str, Any] = {
        "name": cookie["name"],
        "value": cookie.get("value", ""),
        "domain": host,
        "path": cookie.get("path") or "/",
    }
    if cookie.get("expires") is not None:
        out["expires"] = float(cookie["expires"])
    if secure:
        out["secure"] = True
    if cookie.get("httpOnly"):
        out["httpOnly"] = True
    ss = cookie.get("sameSite")
    if ss == "None" and not secure:
        ss = "Lax"
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
