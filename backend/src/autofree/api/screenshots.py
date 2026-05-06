"""截图列表 + 单图查看 — 用于 realtime log 失败时点开看浏览器最后状态。

注意:截图按 stage 命名(`01_login_page_attempt1.png` 等),会被下一个号 overwrite。
要看具体某号失败截图,得在它失败后立即查看。
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
    """列出截图目录下所有 PNG/JPG 文件,按 mtime 降序。"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for p in SCREENSHOT_DIR.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items, "total": len(items)}


@router.get("/file")
def get_screenshot(
    name: str = Query(..., description="文件名(无路径分隔符)"),
    _user=Depends(require_user),
) -> FileResponse:
    """返回指定截图。仅允许 SCREENSHOT_DIR 直接子文件,防路径穿越。"""
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        raise HTTPException(400, "非法文件名")
    path = (SCREENSHOT_DIR / name).resolve()
    try:
        path.relative_to(SCREENSHOT_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "路径越界")
    if not path.is_file():
        raise HTTPException(404, "截图不存在")
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise HTTPException(400, "非图片文件")
    return FileResponse(path, media_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg")
