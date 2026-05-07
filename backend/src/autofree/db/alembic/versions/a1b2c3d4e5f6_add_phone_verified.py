"""add phone_verified columns to account & pending_account

固化 _ensure_phone_verified_columns 的 ALTER —— 防止新部署再踩 schema 缺列的坑。
旧 DB 已被 main.py:_ensure_phone_verified_columns 加过列,这里幂等检测后跳过。

Revision ID: a1b2c3d4e5f6
Revises: 6a4f835d9941
Create Date: 2026-05-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "6a4f835d9941"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, col: str) -> bool:
    insp = sa.inspect(bind)
    return any(c["name"] == col for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("account", "pending_account"):
        if not _has_column(bind, table, "phone_verified"):
            op.add_column(
                table,
                sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            )
            op.create_index(op.f(f"ix_{table}_phone_verified"), table, ["phone_verified"], unique=False)
        if not _has_column(bind, table, "phone_verified_at"):
            op.add_column(
                table,
                sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    for table in ("account", "pending_account"):
        try:
            op.drop_index(op.f(f"ix_{table}_phone_verified"), table_name=table)
        except Exception:
            pass
        try:
            op.drop_column(table, "phone_verified_at")
        except Exception:
            pass
        try:
            op.drop_column(table, "phone_verified")
        except Exception:
            pass
