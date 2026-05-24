"""add phone_e164 to pending_account

phase1 付费但 phase2 失败的 phone-reg 号必须保留手机号,后续用户能用
phone + password 手动登录 chatgpt.com 补救。原 PendingAccount 只存 email,
对 phone-reg 失败号毫无意义(email 没绑,phone 才是真凭证)。

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, col: str) -> bool:
    insp = sa.inspect(bind)
    return any(c["name"] == col for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "pending_account", "phone_e164"):
        op.add_column(
            "pending_account",
            sa.Column("phone_e164", sa.String(32), nullable=False, server_default=""),
        )
        op.create_index(
            op.f("ix_pending_account_phone_e164"),
            "pending_account",
            ["phone_e164"],
            unique=False,
        )


def downgrade() -> None:
    try:
        op.drop_index(op.f("ix_pending_account_phone_e164"), table_name="pending_account")
    except Exception:
        pass
    try:
        op.drop_column("pending_account", "phone_e164")
    except Exception:
        pass
