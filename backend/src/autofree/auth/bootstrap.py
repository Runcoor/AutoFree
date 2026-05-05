"""启动时 bootstrap 密码 — 从 .env APP_PASSWORD 写入 User 表(仅首启)。

之后用户在设置页改了密码就以 DB 为准,env 改不影响。
"""

from __future__ import annotations

import logging

from autofree.auth.service import get_only_user, hash_password
from autofree.db.base import SessionLocal
from autofree.db.models import User
from autofree.settings import get_settings

logger = logging.getLogger(__name__)


def bootstrap_password() -> None:
    """首启 / DB 空时把 .env APP_PASSWORD 写入 User(id=1)。

    env 没配 + DB 也没用户:报警告,允许启动(用户可后续 docker exec 进去手动初始化)。
    DB 有用户:跳过(密码已被用户改过)。
    """
    settings = get_settings()
    with SessionLocal() as db:
        existing = get_only_user(db)
        if existing:
            logger.info("[bootstrap] User(id=1) 已存在,跳过密码 bootstrap")
            return
        if not settings.app_password:
            logger.warning(
                "[bootstrap] DB 无用户且 APP_PASSWORD env 未设;"
                "无法登录,请在 .env 设置 APP_PASSWORD 后重启,或手动 INSERT user 行"
            )
            return
        u = User(id=1, password_hash=hash_password(settings.app_password))
        db.add(u)
        db.commit()
        logger.info("[bootstrap] User(id=1) 已用 APP_PASSWORD 初始化")
