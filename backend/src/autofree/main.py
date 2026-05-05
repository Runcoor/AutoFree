"""FastAPI app 入口 — 路由挂载 + 静态前端 mount + 启动 bootstrap。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from autofree.settings import get_settings

logger = logging.getLogger(__name__)


def _run_migrations() -> None:
    """跑 alembic upgrade head — 启动时自动建表/升级。"""
    from alembic import command
    from alembic.config import Config

    cfg_path = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
    cfg = Config(str(cfg_path))
    command.upgrade(cfg, "head")


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
    _reap_orphan_batches()
    # bootstrap 用户密码(从 .env 读 APP_PASSWORD,首启写 User 表)
    from autofree.auth.bootstrap import bootstrap_password

    bootstrap_password()
    logger.info("AutoFree 启动 data_dir=%s db=%s", settings.data_dir, settings.resolved_database_url)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="AutoFree", version="0.1.0", lifespan=lifespan)

    # ---- API 路由 ----
    from autofree.auth.routes import router as auth_router
    from autofree.api import accounts, domains, freegen, settings as settings_api, sse

    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    app.include_router(settings_api.router, prefix="/api/settings", tags=["settings"])
    app.include_router(domains.router, prefix="/api/domains", tags=["domains"])
    app.include_router(freegen.router, prefix="/api/freegen", tags=["freegen"])
    app.include_router(accounts.router, prefix="/api/accounts", tags=["accounts"])
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
