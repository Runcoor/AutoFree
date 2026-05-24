"""ORM 模型 — User / Setting / Domain / Batch / Account / PendingAccount / Session。"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from autofree.db.base import Base


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Session(Base):
    __tablename__ = "session"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Setting(Base):
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Domain(Base):
    __tablename__ = "domain"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Batch(Base):
    __tablename__ = "batch"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    started_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    accounts: Mapped[list["Account"]] = relationship(back_populates="batch")
    pendings: Mapped[list["PendingAccount"]] = relationship(back_populates="batch")


class Account(Base):
    __tablename__ = "account"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batch.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), default="")
    plan_type: Mapped[str] = mapped_column(String(32), default="free")
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    id_token: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refresh: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_json_path: Mapped[str] = mapped_column(String(512), default="")
    cpa_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    cpa_synced_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cpa_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 此号是否已通过手机验证(5sim 实际扣过费 OR 历史已验证 → OpenAI 不再要求 phone gate)。
    # 用途:pending 列表里突显已付费号,告诉用户「这号花过钱,优先 resume,别浪费」
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    phone_verified_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # phone-reg 注册的号的手机号(E.164,例 +5585999700974);email-reg 走默认空串
    phone_e164: Mapped[str] = mapped_column(String(32), default="", nullable=False, index=True)
    # 此号是否绑了 email(可走邮件 reauth)。
    # - email-reg → True(默认)
    # - phone-reg + /add-email 成功 → True
    # - phone-reg 但 OAuth 走 picker shortcut 没触发 /add-email → False
    #   未绑号下次 reauth 必须再花钱跑 SMS,需要用户手动到 settings 补绑
    email_bound: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    batch: Mapped[Batch] = relationship(back_populates="accounts")


class PendingAccount(Base):
    __tablename__ = "pending_account"
    __table_args__ = (UniqueConstraint("email", name="uq_pending_email"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batch.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    error_kind: Mapped[str] = mapped_column(String(64), default="", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    # 5sim 真实扣过费 / 历史已验证 → 此号已通过手机验证。resume 时优先,绝不能丢
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    phone_verified_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # phone-reg 失败后必须保留手机号 — 这号花了 SMS 钱,后续用户要手动登录
    # OpenAI 补救。email 只是 cloud-mail 占位,真正能登录的凭证是 phone + password
    phone_e164: Mapped[str] = mapped_column(String(32), default="", nullable=False, index=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_via: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="pendings")
