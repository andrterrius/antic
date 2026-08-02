"""User accounts stored in a private JSON file (never exposed via library routes)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_USERNAME_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё_.-]{2,64}$")
_PBKDF2_ITERATIONS = 310_000
_SALT_BYTES = 16


@dataclass(frozen=True)
class UserRecord:
    username: str
    password_hash: str
    locale: str = "ru"
    is_admin: bool = False
    created_at: float = 0.0

    def public_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "locale": self.locale,
            "is_admin": self.is_admin,
        }


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt_b = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_b,
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt_b.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iter_s, salt_hex, hash_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return secrets.compare_digest(digest, expected)


def normalize_username(raw: str) -> str:
    return (raw or "").strip()


def validate_username(username: str) -> str:
    name = normalize_username(username)
    if not _USERNAME_RE.match(name):
        raise ValueError(
            "Логин: 2–64 символа — латиница, кириллица, цифры, _ . -"
        )
    return name


def validate_password(password: str) -> str:
    pw = password if isinstance(password, str) else ""
    if len(pw) < 4:
        raise ValueError("Password must be at least 4 characters")
    if len(pw) > 256:
        raise ValueError("Password is too long")
    return pw


def validate_locale(locale: str) -> str:
    loc = (locale or "").strip().lower() or "ru"
    if loc not in {"ru", "en"}:
        raise ValueError("Unsupported locale (use ru or en)")
    return loc


def _secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        if os.name != "nt":
            os.chmod(path, 0o700)
    except OSError:
        pass


def _secure_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    try:
        if os.name != "nt":
            os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        pass


class UsersStore:
    """Thread-safe users.json under the private data directory."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        _secure_mkdir(self._path.parent)
        self._users: dict[str, UserRecord] = {}
        self._mtime: float | None = None
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _stat_mtime(self) -> float | None:
        try:
            if not self._path.is_file():
                return None
            return self._path.stat().st_mtime
        except OSError:
            return None

    def _load(self) -> None:
        mtime = self._stat_mtime()
        if mtime is None:
            self._users = {}
            self._mtime = None
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            self._mtime = mtime
            return
        items = raw.get("users") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            self._users = {}
            self._mtime = mtime
            return
        loaded: dict[str, UserRecord] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = normalize_username(str(item.get("username") or ""))
            ph = str(item.get("password_hash") or "")
            if not name or not ph:
                continue
            try:
                loc = validate_locale(str(item.get("locale") or "ru"))
            except ValueError:
                loc = "ru"
            loaded[name.lower()] = UserRecord(
                username=name,
                password_hash=ph,
                locale=loc,
                is_admin=bool(item.get("is_admin")),
                created_at=float(item.get("created_at") or 0.0) or time.time(),
            )
        self._users = loaded
        self._mtime = mtime

    def _reload_if_stale_unlocked(self) -> list[str]:
        """
        Re-read users.json when the file changes on disk (e.g. manual edit).
        Returns lowercase usernames that disappeared since the previous load.
        """
        mtime = self._stat_mtime()
        if mtime == self._mtime:
            return []
        before = set(self._users.keys())
        self._load()
        return sorted(before - set(self._users.keys()))

    def reload_if_stale(self) -> list[str]:
        with self._lock:
            return self._reload_if_stale_unlocked()

    def _dump(self) -> None:
        payload = {
            "users": [
                {
                    "username": u.username,
                    "password_hash": u.password_hash,
                    "locale": u.locale,
                    "is_admin": u.is_admin,
                    "created_at": u.created_at,
                }
                for u in sorted(self._users.values(), key=lambda x: x.username.lower())
            ]
        }
        _secure_write_json(self._path, payload)
        self._mtime = self._stat_mtime()

    def list_users(self) -> list[UserRecord]:
        with self._lock:
            self._reload_if_stale_unlocked()
            return sorted(self._users.values(), key=lambda u: u.username.lower())

    def get(self, username: str) -> UserRecord | None:
        key = normalize_username(username).lower()
        with self._lock:
            self._reload_if_stale_unlocked()
            return self._users.get(key)

    def authenticate(self, username: str, password: str) -> UserRecord | None:
        user = self.get(username)
        if user is None:
            # Constant-time-ish dummy work to reduce username oracle timing.
            verify_password(password or "x", _hash_password("dummy"))
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def create_user(
        self,
        username: str,
        password: str,
        *,
        locale: str = "ru",
        is_admin: bool = False,
    ) -> UserRecord:
        name = validate_username(username)
        pw = validate_password(password)
        loc = validate_locale(locale)
        with self._lock:
            self._reload_if_stale_unlocked()
            if name.lower() in self._users:
                raise ValueError("User already exists")
            user = UserRecord(
                username=name,
                password_hash=_hash_password(pw),
                locale=loc,
                is_admin=is_admin,
                created_at=time.time(),
            )
            self._users[name.lower()] = user
            self._dump()
            return user

    def set_locale(self, username: str, locale: str) -> UserRecord:
        loc = validate_locale(locale)
        with self._lock:
            self._reload_if_stale_unlocked()
            user = self._users.get(normalize_username(username).lower())
            if user is None:
                raise ValueError("User not found")
            updated = UserRecord(
                username=user.username,
                password_hash=user.password_hash,
                locale=loc,
                is_admin=user.is_admin,
                created_at=user.created_at,
            )
            self._users[user.username.lower()] = updated
            self._dump()
            return updated

    def set_password(self, username: str, password: str) -> UserRecord:
        pw = validate_password(password)
        with self._lock:
            self._reload_if_stale_unlocked()
            user = self._users.get(normalize_username(username).lower())
            if user is None:
                raise ValueError("User not found")
            updated = UserRecord(
                username=user.username,
                password_hash=_hash_password(pw),
                locale=user.locale,
                is_admin=user.is_admin,
                created_at=user.created_at,
            )
            self._users[user.username.lower()] = updated
            self._dump()
            return updated

    def delete_user(self, username: str) -> UserRecord:
        """Remove a user. Raises ValueError if missing or last admin."""
        key = normalize_username(username).lower()
        with self._lock:
            self._reload_if_stale_unlocked()
            user = self._users.get(key)
            if user is None:
                raise ValueError("Пользователь не найден")
            if user.is_admin:
                admins = sum(1 for u in self._users.values() if u.is_admin)
                if admins <= 1:
                    raise ValueError("Нельзя удалить последнего администратора")
            del self._users[key]
            self._dump()
            return user

    def ensure_bootstrap_admin(
        self, *, username: str = "admin", password: str
    ) -> UserRecord | None:
        """Create the first admin if the store is empty. Returns created user or None."""
        with self._lock:
            self._reload_if_stale_unlocked()
            if self._users:
                return None
        return self.create_user(username, password, locale="ru", is_admin=True)


@dataclass(frozen=True)
class SessionRecord:
    token: str
    username: str
    created_at: float
    expires_at: float


class SessionStore:
    """Opaque session tokens persisted under the private directory."""

    def __init__(self, path: Path, *, ttl_seconds: int = 60 * 60 * 24 * 14) -> None:
        self._path = Path(path)
        self._ttl = max(3600, int(ttl_seconds))
        self._lock = threading.RLock()
        _secure_mkdir(self._path.parent)
        self._sessions: dict[str, SessionRecord] = {}
        self._mtime: float | None = None
        self._load()

    def _stat_mtime(self) -> float | None:
        try:
            if not self._path.is_file():
                return None
            return self._path.stat().st_mtime
        except OSError:
            return None

    def _load(self) -> None:
        mtime = self._stat_mtime()
        if mtime is None:
            self._sessions = {}
            self._mtime = None
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            self._mtime = mtime
            return
        items = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            self._sessions = {}
            self._mtime = mtime
            return
        now = time.time()
        loaded: dict[str, SessionRecord] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            token = str(item.get("token") or "")
            username = normalize_username(str(item.get("username") or ""))
            if not token or not username:
                continue
            expires = float(item.get("expires_at") or 0.0)
            if expires and expires < now:
                continue
            loaded[token] = SessionRecord(
                token=token,
                username=username,
                created_at=float(item.get("created_at") or now),
                expires_at=expires or (now + self._ttl),
            )
        self._sessions = loaded
        self._mtime = mtime

    def _reload_if_stale_unlocked(self) -> None:
        mtime = self._stat_mtime()
        if mtime == self._mtime:
            return
        self._load()

    def _dump(self) -> None:
        payload = {
            "sessions": [
                {
                    "token": s.token,
                    "username": s.username,
                    "created_at": s.created_at,
                    "expires_at": s.expires_at,
                }
                for s in self._sessions.values()
            ]
        }
        _secure_write_json(self._path, payload)
        self._mtime = self._stat_mtime()

    def create(self, username: str) -> SessionRecord:
        now = time.time()
        session = SessionRecord(
            token=secrets.token_urlsafe(32),
            username=normalize_username(username),
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._reload_if_stale_unlocked()
            self._sessions[session.token] = session
            self._dump()
        return session

    def get(self, token: str) -> SessionRecord | None:
        tok = (token or "").strip()
        if not tok:
            return None
        with self._lock:
            self._reload_if_stale_unlocked()
            session = self._sessions.get(tok)
            if session is None:
                return None
            if session.expires_at < time.time():
                self._sessions.pop(tok, None)
                self._dump()
                return None
            return session

    def revoke(self, token: str) -> None:
        tok = (token or "").strip()
        if not tok:
            return
        with self._lock:
            self._reload_if_stale_unlocked()
            if tok in self._sessions:
                self._sessions.pop(tok, None)
                self._dump()

    def revoke_user(self, username: str) -> None:
        key = normalize_username(username).lower()
        with self._lock:
            self._reload_if_stale_unlocked()
            drop = [t for t, s in self._sessions.items() if s.username.lower() == key]
            if not drop:
                return
            for t in drop:
                self._sessions.pop(t, None)
            self._dump()
