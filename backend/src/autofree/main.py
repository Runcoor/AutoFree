"""FastAPI app 入口 — 路由挂载 + 静态前端 mount + 启动 bootstrap。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from autofree.settings import get_settings


def _setup_logging() -> None:
    """配置 root logger — 否则默认 WARNING,autofree.* 的 INFO 不输出。

    LOG_LEVEL 环境变量可覆盖(默认 INFO)。统一格式带 logger 名,方便 grep。
    """
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    # force=True 覆盖 uvicorn / 其他库可能预设的 handler,保证我们的格式生效
    logging.basicConfig(
        level=level,
        format="%(levelname)-5s [%(name)s] %(message)s",
        force=True,
    )


_setup_logging()

logger = logging.getLogger(__name__)


def _run_migrations() -> None:
    """跑 alembic upgrade head — 启动时自动建表/升级。"""
    from alembic import command
    from alembic.config import Config

    cfg_path = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
    cfg = Config(str(cfg_path))
    command.upgrade(cfg, "head")


def _ensure_phone_verified_columns() -> None:
    """轻量 schema 升级:给 account / pending_account 加 phone_verified 列(若缺)。

    我们用 Alembic 跑迁移,但这个简单 ALTER 在 SQLite 下做幂等检测更省事。
    SQLite 不支持 IF NOT EXISTS 加列,所以 try/except 吃掉 OperationalError。
    """
    from sqlalchemy import text
    from autofree.db.base import SessionLocal

    statements = [
        ("account", "phone_verified", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("account", "phone_verified_at", "DATETIME"),
        ("pending_account", "phone_verified", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("pending_account", "phone_verified_at", "DATETIME"),
        # phone-reg 注册号的手机号 + 是否已绑邮箱 — 用于后续手动补绑识别
        ("account", "phone_e164", "VARCHAR(32) DEFAULT '' NOT NULL"),
        ("account", "email_bound", "BOOLEAN DEFAULT 1 NOT NULL"),
    ]
    with SessionLocal() as db:
        for table, col, coldef in statements:
            try:
                db.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}"))
                db.commit()
                logger.info("[bootstrap] 加列 %s.%s", table, col)
            except Exception:
                db.rollback()  # 已存在 → SQLite 抛 OperationalError,忽略即可


def _reap_orphan_batches() -> None:
    """启动时把所有 status=running 的 Batch 标 stopped。

    上次进程被 kill / 容器重启 → 进程内的 thread 没了,但 DB 里那行还卡在 running,
    UI 会永久显示「运行中」且新批次启动会被「已有任务在运行」拦住。
    """
    import datetime as _dt
    from sqlalchemy import select
    from autofree.db.base import SessionLocal
    from autofree.db.models import Batch

    with SessionLocal() as db:
        orphans = db.execute(select(Batch).where(Batch.status == "running")).scalars().all()
        if not orphans:
            return
        now = _dt.datetime.now(_dt.timezone.utc)
        for b in orphans:
            logger.warning("[bootstrap] 修复孤儿 running 批次 %s (started=%s) → stopped",
                           b.id, b.started_at)
            b.status = "stopped"
            b.finished_at = now
        db.commit()
        logger.info("[bootstrap] 已修复 %d 个孤儿批次", len(orphans))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    _run_migrations()
    _ensure_phone_verified_columns()
    _reap_orphan_batches()
    try:
        from autofree.core.browser import cleanup_old_screenshots
        cleanup_old_screenshots(days=7)
    except Exception:
        logger.exception("[bootstrap] cleanup_old_screenshots 失败(忽略)")
    # bootstrap 用户密码(从 .env 读 APP_PASSWORD,首启写 User 表)
    from autofree.auth.bootstrap import bootstrap_password

    bootstrap_password()
    logger.info("AutoFree 启动 data_dir=%s db=%s", settings.data_dir, settings.resolved_database_url)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="AutoFree", version="0.1.0", lifespan=lifespan)

    # ---- API 路由 ----
    from autofree.auth.routes import router as auth_router
    from autofree.api import accounts, domains, freegen, screenshots, settings as settings_api, sse

    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    app.include_router(settings_api.router, prefix="/api/settings", tags=["settings"])
    app.include_router(domains.router, prefix="/api/domains", tags=["domains"])
    app.include_router(freegen.router, prefix="/api/freegen", tags=["freegen"])
    app.include_router(accounts.router, prefix="/api/accounts", tags=["accounts"])
    app.include_router(screenshots.router, prefix="/api/screenshots", tags=["screenshots"])
    app.include_router(sse.router, prefix="/api/sse", tags=["sse"])

    # ---- 前端 SPA mount(优先 dist,缺则 fallback 到 dev 提示)----
    static_dir = Path(__file__).resolve().parent.parent.parent / "static"
    if static_dir.exists():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_index(full_path: str):
            # API 路径已被前面 router 截走;这里只伺服 SPA index.html
            target = static_dir / full_path
            if target.is_file():
                return FileResponse(target)
            return FileResponse(static_dir / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def dev_landing():
            return {"status": "ok", "msg": "frontend not built; run frontend dev server separately"}

    return app


app = create_app()
