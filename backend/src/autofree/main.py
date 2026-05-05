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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
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
