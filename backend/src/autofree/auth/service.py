"""auth 业务逻辑 — bcrypt + Session token。

约定:
- 单用户 (User.id=1)
- session token 是随机 32 字节 → urlsafe base64;DB 存 sha256 hash
- 修改密码后,该用户全部 session 强制失效
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import secrets

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from autofree.db.models import Session as SessionRow
from autofree.db.models import User
from autofree.settings import get_settings

logger = logging.getLogger(__name__)


def hash_password(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("密码不可为空")
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, stored_hash: str) -> bool:
    if not plaintext or not stored_hash:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode(), stored_hash.encode())
    except Exception:
        return False


def get_only_user(db: Session) -> User | None:
    return db.execute(select(User).where(User.id == 1)).scalar_one_or_none()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User) -> str:
    """新签一个 session token,DB 存 hash,返明文给调用方写 cookie。"""
    token = secrets.token_urlsafe(32)
    settings = get_settings()
    expires = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=settings.session_lifetime_days)
    db.add(SessionRow(user_id=user.id, token_hash=_hash_token(token), expires_at=expires))
    db.commit()
    return token


def lookup_session(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    row = db.execute(
        select(SessionRow).where(SessionRow.token_hash == _hash_token(token))
    ).scalar_one_or_none()
    if not row:
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    expires = row.expires_at
    # SQLite 可能返回 naive datetime,统一为 aware
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=_dt.timezone.utc)
    if expires < now:
        db.delete(row)
        db.commit()
        return None
    return db.execute(select(User).where(User.id == row.user_id)).scalar_one_or_none()


def revoke_session(db: Session, token: str | None) -> None:
    if not token:
        return
    row = db.execute(
        select(SessionRow).where(SessionRow.token_hash == _hash_token(token))
    ).scalar_one_or_none()
    if row:
        db.delete(row)
        db.commit()


def revoke_all_sessions(db: Session, user_id: int) -> int:
    rows = db.execute(select(SessionRow).where(SessionRow.user_id == user_id)).scalars().all()
    for r in rows:
        db.delete(r)
    db.commit()
    return len(rows)


def change_password(db: Session, user: User, new_plaintext: str) -> None:
    user.password_hash = hash_password(new_plaintext)
    db.commit()
    revoke_all_sessions(db, user.id)
