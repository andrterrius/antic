"""Auth stores and private directory for Antidetect serve/web mode."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from users_auth import (
    SessionRecord,
    SessionStore,
    UserRecord,
    UsersStore,
    normalize_username,
    validate_username,
)

ENV_PRIVATE_DIR = "ANTIDETECT_PRIVATE_DIR"
ENV_ZALIVER_PRIVATE_DIR = "ZALIVER_API_PRIVATE_DIR"
ENV_MULTIUSER = "ANTIDETECT_MULTIUSER"


def _default_private_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        if root:
            return Path(root) / "Antidetect" / "private"
    return Path.home() / ".antidetect" / "private"


def _default_zaliver_private_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        if root:
            return Path(root) / "Zaliver" / "private"
    return Path.home() / ".zaliver" / "private"


def private_dir() -> Path:
    raw = (os.environ.get(ENV_PRIVATE_DIR) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _default_private_dir()


def zaliver_private_dir() -> Path:
    raw = (os.environ.get(ENV_ZALIVER_PRIVATE_DIR) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _default_zaliver_private_dir()


def multiuser_enabled() -> bool:
    """Per-user profile roots — on for serve/web; off for desktop Qt."""
    raw = (os.environ.get(ENV_MULTIUSER) or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # Default: on when API token / serve auth is configured.
    return bool((os.environ.get("ANTIDETECT_API_TOKEN") or "").strip())


_lock = threading.RLock()
_users: UsersStore | None = None
_sessions: SessionStore | None = None


def get_users_store() -> UsersStore:
    global _users
    with _lock:
        if _users is None:
            path = private_dir() / "users.json"
            _users = UsersStore(path)
            if not _users.list_users():
                pw = (os.environ.get("ANTIDETECT_ADMIN_PASSWORD") or "").strip() or "admin"
                created = _users.ensure_bootstrap_admin(username="admin", password=pw)
                if created is not None and pw == "admin":
                    print(
                        "WARNING: Antidetect bootstrap admin password is 'admin'. "
                        "Set ANTIDETECT_ADMIN_PASSWORD.",
                        flush=True,
                    )
        return _users


def get_sessions_store() -> SessionStore:
    global _sessions
    with _lock:
        if _sessions is None:
            _sessions = SessionStore(private_dir() / "sessions.json")
        return _sessions


def lookup_zaliver_session(token: str) -> str | None:
    """
    Resolve username from Zaliver's opaque session token
    (%LOCALAPPDATA%\\Zaliver\\private\\sessions.json).
    Same token format as Zaliver web login — no shared password file.
    """
    tok = (token or "").strip()
    if not tok:
        return None
    path = zaliver_private_dir() / "sessions.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    items = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return None
    now = time.time()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("token") or "") != tok:
            continue
        expires = float(item.get("expires_at") or 0.0)
        if expires and expires < now:
            return None
        name = normalize_username(str(item.get("username") or ""))
        return name or None
    return None


def ensure_local_user_for_external(username: str) -> UserRecord:
    """
    Ensure an antidetect user row exists for a Zaliver username
    (separate users.json; password may be empty-hash placeholder for SSO-only).
    """
    store = get_users_store()
    existing = store.get(username)
    if existing is not None:
        return existing
    # Create with a random unusable password — login via Zaliver token only unless admin resets.
    import secrets

    try:
        name = validate_username(username)
    except ValueError:
        # Zaliver might have edge names; still scope data by sanitized key.
        name = normalize_username(username)[:64] or "user"
    try:
        return store.create_user(
            name,
            secrets.token_urlsafe(24),
            locale="ru",
            is_admin=False,
        )
    except ValueError:
        # Race: another request created it
        hit = store.get(name)
        if hit is None:
            raise
        return hit
