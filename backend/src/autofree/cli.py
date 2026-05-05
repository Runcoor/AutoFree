"""autofree CLI 入口 — 起 uvicorn 跑 web service。

```bash
autofree              # 起 web (默认 0.0.0.0:8000)
autofree --port 9000  # 改端口
autofree migrate      # 跑 alembic upgrade head
```
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="autofree", description="批量自动注册 OpenAI free 账号")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="启动 web service (默认)")
    p_run.add_argument("--host", default="0.0.0.0")
    p_run.add_argument("--port", type=int, default=8000)
    p_run.add_argument("--reload", action="store_true", help="dev 热重载")

    sub.add_parser("migrate", help="跑 alembic upgrade head 创建/升级 schema")

    args, _ = parser.parse_known_args()
    cmd = args.cmd or "run"

    if cmd == "migrate":
        from alembic import command
        from alembic.config import Config
        from pathlib import Path

        cfg_path = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
        cfg = Config(str(cfg_path))
        command.upgrade(cfg, "head")
        return

    if cmd == "run":
        import uvicorn

        uvicorn.run(
            "autofree.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return

    parser.print_help()
    sys.exit(1)
