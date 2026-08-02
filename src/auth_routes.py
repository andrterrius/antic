"""Login / logout / me / admin user CRUD for Antidetect web."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from api_auth import AuthContext, auth_from_request, require_api_token
from auth_runtime import get_sessions_store, get_users_store
from users_auth import validate_locale, validate_password, validate_username


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    user: dict[str, Any]


class UserPublic(BaseModel):
    username: str
    locale: str
    is_admin: bool = False


class PatchMeRequest(BaseModel):
    locale: str | None = None
    password: str | None = None


class CreateUserRequest(BaseModel):
    username: str
    password: str
    locale: str = "ru"
    is_admin: bool = False


def build_auth_router() -> APIRouter:
    router = APIRouter(tags=["Авторизация"])

    @router.post("/auth/login", response_model=LoginResponse)
    def login(body: LoginRequest) -> LoginResponse:
        user = get_users_store().authenticate(body.username, body.password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверный логин или пароль",
            )
        session = get_sessions_store().create(user.username)
        return LoginResponse(token=session.token, user=user.public_dict())

    @router.post("/auth/logout")
    def logout(
        request: Request,
        _auth: AuthContext | None = Depends(require_api_token),
    ) -> dict[str, bool]:
        ctx = auth_from_request(request)
        if ctx is not None and ctx.source == "session":
            get_sessions_store().revoke(ctx.token)
        return {"ok": True}

    @router.get("/auth/me", response_model=UserPublic)
    def me(
        request: Request,
        _auth: AuthContext | None = Depends(require_api_token),
    ) -> UserPublic:
        ctx = auth_from_request(request)
        if ctx is None:
            raise HTTPException(status_code=401, detail="Требуется авторизация")
        fresh = get_users_store().get(ctx.user.username) or ctx.user
        return UserPublic(**fresh.public_dict())

    @router.patch("/auth/me", response_model=UserPublic)
    def patch_me(
        body: PatchMeRequest,
        request: Request,
        _auth: AuthContext | None = Depends(require_api_token),
    ) -> UserPublic:
        ctx = auth_from_request(request)
        if ctx is None:
            raise HTTPException(status_code=401, detail="Требуется авторизация")
        user = ctx.user
        try:
            if body.locale is not None:
                user = get_users_store().set_locale(user.username, body.locale)
            if body.password is not None:
                user = get_users_store().set_password(user.username, body.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return UserPublic(**user.public_dict())

    @router.get("/auth/users", response_model=list[UserPublic])
    def list_users(
        request: Request,
        _auth: AuthContext | None = Depends(require_api_token),
    ) -> list[UserPublic]:
        ctx = auth_from_request(request)
        if ctx is None or not ctx.user.is_admin:
            raise HTTPException(status_code=403, detail="Только для администратора")
        return [UserPublic(**u.public_dict()) for u in get_users_store().list_users()]

    @router.post(
        "/auth/users",
        response_model=UserPublic,
        status_code=status.HTTP_201_CREATED,
    )
    def create_user(
        body: CreateUserRequest,
        request: Request,
        _auth: AuthContext | None = Depends(require_api_token),
    ) -> UserPublic:
        ctx = auth_from_request(request)
        if ctx is None or not ctx.user.is_admin:
            raise HTTPException(status_code=403, detail="Только для администратора")
        try:
            validate_username(body.username)
            validate_password(body.password)
            loc = validate_locale(body.locale)
            user = get_users_store().create_user(
                body.username,
                body.password,
                locale=loc,
                is_admin=bool(body.is_admin),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return UserPublic(**user.public_dict())

    @router.delete("/auth/users/{username}")
    def delete_user(
        username: str,
        request: Request,
        _auth: AuthContext | None = Depends(require_api_token),
    ) -> dict[str, Any]:
        ctx = auth_from_request(request)
        if ctx is None or not ctx.user.is_admin:
            raise HTTPException(status_code=403, detail="Только для администратора")
        if username.strip().lower() == ctx.user.username.lower():
            raise HTTPException(status_code=400, detail="Нельзя удалить свой аккаунт")
        try:
            deleted = get_users_store().delete_user(username)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        get_sessions_store().revoke_user(deleted.username)
        from profiles_store import purge_user_storage

        purged = purge_user_storage(deleted.username)
        return {"ok": True, "username": deleted.username, "purged_data": purged}

    return router
