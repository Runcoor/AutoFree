"""FastAPI dependencies — 当前 session / 必登认证。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from autofree.auth.service import lookup_session
from autofree.db.base import get_db
from autofree.db.models import User
from autofree.settings import get_settings


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    return lookup_session(db, token)


def require_user(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="未登录或 session 已失效")
    return user
