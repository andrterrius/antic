"""Bearer authentication for Antidetect HTTP API.

Desktop Qt (no token configured): open access, shared legacy storage.

Serve / web (ANTIDETECT_API_TOKEN set):
  1. Antidetect session Bearer (login/password → sessions.json)
  2. Zaliver session Bearer (same opaque token from Zaliver private/sessions.json)
  3. Machine token ANTIDETECT_API_TOKEN (default ``secret``) → admin scope
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth_runtime import (
    get_sessions_store,
    get_users_store,
    lookup_zaliver_session,
    multiuser_enabled,
)
from profiles_store import set_data_username, user_data_scope
from users_auth import UserRecord

ENV_TOKEN = "ANTIDETECT_API_TOKEN"

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    user: UserRecord
    token: str
    source: str  # session | zaliver | machine | open


def get_configured_token() -> str | None:
    t = (os.environ.get(ENV_TOKEN) or "").strip()
    return t or None


def clear_api_token() -> None:
    """Remove token from process env (open access)."""
    os.environ.pop(ENV_TOKEN, None)


def ensure_api_token(*, explicit: str | None = None) -> tuple[str, bool]:
    """
    Resolve API token and set ANTIDETECT_API_TOKEN in the process env.

    Returns (token, generated) where generated=True if the default ``secret`` was applied.
    Priority: explicit argument → existing env → default ``secret``.

    Default ``secret`` matches Zaliver's historical antydetect/local_api_token
    so cross-app Bearer still works for automation / desktop bridge.
    """
    if explicit is not None and str(explicit).strip():
        token = str(explicit).strip()
        os.environ[ENV_TOKEN] = token
        return token, False
    existing = get_configured_token()
    if existing:
        return existing, False
    token = "secret"
    os.environ[ENV_TOKEN] = token
    return token, True


def _sync_users_and_revoke_removed() -> None:
    """Pick up users.json edits and drop sessions for accounts removed from the file."""
    removed = get_users_store().reload_if_stale()
    if not removed:
        return
    sessions = get_sessions_store()
    for name in removed:
        sessions.revoke_user(name)


def _resolve_bearer(provided: str) -> AuthContext:
    _sync_users_and_revoke_removed()
    sessions = get_sessions_store()
    users = get_users_store()

    session = sessions.get(provided)
    if session is not None:
        user = users.get(session.username)
        if user is not None:
            return AuthContext(user=user, token=provided, source="session")
        # User deleted from users.json (or store) — kill orphan session, deny access.
        sessions.revoke(provided)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь удалён или сессия недействительна",
            headers={"WWW-Authenticate": "Bearer"},
        )

    zaliver_user = lookup_zaliver_session(provided)
    if zaliver_user:
        # Do not auto-recreate: deleting the local user must cut off access.
        user = users.get(zaliver_user)
        if user is not None:
            return AuthContext(user=user, token=provided, source="zaliver")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь удалён или не зарегистрирован",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected = get_configured_token()
    if expected and secrets.compare_digest(provided, expected):
        admin = users.get("admin") or next(
            (u for u in users.list_users() if u.is_admin),
            None,
        )
        if admin is None:
            users.ensure_bootstrap_admin(username="admin", password="admin")
            admin = users.get("admin")
        if admin is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return AuthContext(user=admin, token=provided, source="machine")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Сессия недействительна или неверный токен",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext | None:
    """
    FastAPI dependency. Desktop without token → open. Serve → Bearer + per-user scope.

    Note: uses ContextVar without yield/reset — FastAPI may run sync deps on a
    threadpool; always overwrite scope at the start of each authenticated request.
    """
    expected = get_configured_token()
    if not expected:
        request.state.antidetect_auth = None  # type: ignore[attr-defined]
        set_data_username(None)
        return None

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
            headers={"WWW-Authenticate": "Bearer"},
        )
    provided = (credentials.credentials or "").strip()
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
            headers={"WWW-Authenticate": "Bearer"},
        )

    ctx = _resolve_bearer(provided)
    request.state.antidetect_auth = ctx  # type: ignore[attr-defined]

    if multiuser_enabled():
        set_data_username(ctx.user.username)
    else:
        set_data_username(None)
    return ctx


def auth_from_request(request: Request) -> AuthContext | None:
    return getattr(request.state, "antidetect_auth", None)
