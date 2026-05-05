"""auth 路由 — /me /login /logout /change-password。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from autofree.auth.service import (
    change_password,
    create_session,
    get_only_user,
    lookup_session,
    revoke_session,
    verify_password,
)
from autofree.db.base import get_db
from autofree.deps import require_user
from autofree.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginParams(BaseModel):
    password: str = Field(min_length=1)


class ChangePasswordParams(BaseModel):
    old: str = Field(min_length=1)
    new: str = Field(min_length=4, max_length=128)


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    user = lookup_session(db, token)
    return {
        "authenticated": user is not None,
        "user_id": user.id if user else None,
    }


@router.post("/login")
def login(params: LoginParams, response: Response, db: Session = Depends(get_db)) -> dict:
    user = get_only_user(db)
    if not user:
        raise HTTPException(503, "应用尚未初始化:请在 .env 设置 APP_PASSWORD 后重启")
    if not verify_password(params.password, user.password_hash):
        # 不告诉是哪个错(用户/密码)— 减少信息泄露
        raise HTTPException(401, "密码错误")

    token = create_session(db, user)
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_lifetime_days * 86400,
        httponly=True,
        samesite="lax",
        secure=False,  # 部署到 HTTPS 时改 True (反代/env 控制)
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    revoke_session(db, token)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password_route(
    params: ChangePasswordParams,
    response: Response,
    user=Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(params.old, user.password_hash):
        raise HTTPException(401, "旧密码错误")
    change_password(db, user, params.new)
    settings = get_settings()
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True, "msg": "密码已更新,请重新登录"}
