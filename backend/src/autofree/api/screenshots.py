"""截图列表 + 单图查看 — 用于 realtime log 失败时点开看浏览器最后状态。

布局:`<SCREENSHOT_DIR>/<YYMMDD_HHMMSS>_<email-prefix>/<stage>.png` —
每个号一个独立子目录,避免不同号互相覆盖。

API:
- GET /api/screenshots          列出所有截图(name 是相对 SCREENSHOT_DIR 的相对路径)
- GET /api/screenshots/file?name=<rel> 取单图
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from autofree.core.config import SCREENSHOT_DIR
from autofree.deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
def list_screenshots(_user=Depends(require_user)) -> dict:
    """递归列出 SCREENSHOT_DIR 下所有图片,name 用相对路径(支持嵌套)。"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    base = SCREENSHOT_DIR.resolve()
    for p in SCREENSHOT_DIR.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        try:
            st = p.stat()
            rel = p.resolve().relative_to(base)
        except (OSError, ValueError):
            continue
        items.append({
            "name": str(rel),  # 相对路径 (e.g. "20260507_021530_user1/03_after_email.png")
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items, "total": len(items)}


@router.get("/file")
def get_screenshot(
    name: str = Query(..., description="相对 SCREENSHOT_DIR 的路径,允许子目录"),
    _user=Depends(require_user),
) -> FileResponse:
    """返回指定截图。允许嵌套子目录,但严格防止路径穿越。"""
    if "\\" in name or name.startswith("/") or name.startswith(".") or ".." in name.split("/"):
        raise HTTPException(400, "非法路径")
    path = (SCREENSHOT_DIR / name).resolve()
    base = SCREENSHOT_DIR.resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise HTTPException(400, "路径越界")
    if not path.is_file():
        raise HTTPException(404, "截图不存在")
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise HTTPException(400, "非图片文件")
    return FileResponse(path, media_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg")
