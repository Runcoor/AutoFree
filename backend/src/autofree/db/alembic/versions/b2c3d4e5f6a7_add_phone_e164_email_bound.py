"""add phone_e164 + email_bound columns to account

phone-reg 注册的号需要持久化手机号 + 邮箱绑定状态:
  - phone_e164    — 注册时使用的手机号(E.164),空表示 email-reg
  - email_bound   — 是否已绑邮箱;phone-reg 走 OAuth picker shortcut 时
                    可能跳过 /add-email,此时为 False,提示用户手动补绑

幂等检测列是否存在,允许旧 DB 升级到新 schema 不踩坑。

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, col: str) -> bool:
    insp = sa.inspect(bind)
    return any(c["name"] == col for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "account", "phone_e164"):
        op.add_column(
            "account",
            sa.Column("phone_e164", sa.String(32), nullable=False, server_default=""),
        )
        op.create_index(op.f("ix_account_phone_e164"), "account", ["phone_e164"], unique=False)
    if not _has_column(bind, "account", "email_bound"):
        op.add_column(
            "account",
            sa.Column("email_bound", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        )
        op.create_index(op.f("ix_account_email_bound"), "account", ["email_bound"], unique=False)


def downgrade() -> None:
    try:
        op.drop_index(op.f("ix_account_email_bound"), table_name="account")
    except Exception:
        pass
    try:
        op.drop_column("account", "email_bound")
    except Exception:
        pass
    try:
        op.drop_index(op.f("ix_account_phone_e164"), table_name="account")
    except Exception:
        pass
    try:
        op.drop_column("account", "phone_e164")
    except Exception:
        pass
