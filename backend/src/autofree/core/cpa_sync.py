"""CPA (CLIProxyAPI) 单向上传 — autofree 精简版。

抽自 AutoTeam-F/src/autoteam/cpa_sync.py,只保留 upload_to_cpa 一个动作:
- 拉 CPA url/key 从 DB Setting 表
- POST multipart 上传认证文件
- 返 (success, message)

不做反向 sync(那是 autoteam 的活)。
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from autofree.core.config import get_cpa_config

logger = logging.getLogger(__name__)


def is_cpa_configured() -> bool:
    cfg = get_cpa_config()
    return bool(cfg["url"] and cfg["key"] and cfg["enabled"])


def upload_to_cpa(filepath: str | Path, *, timeout: int = 15) -> tuple[bool, str]:
    """上传 codex auth JSON 到 CPA。

    返 (ok, message)。文件不存在 / CPA 未配 / HTTP 失败都返 False。
    """
    cfg = get_cpa_config()
    if not (cfg["url"] and cfg["key"]):
        return False, "CPA url/key 未配置"
    if not cfg["enabled"]:
        return False, "CPA push 已在设置里关闭"

    p = Path(filepath)
    if not p.exists():
        return False, f"文件不存在: {p}"

    url = cfg["url"].rstrip("/") + "/v0/management/auth-files"
    headers = {"Authorization": f"Bearer {cfg['key']}"}

    try:
        with p.open("rb") as f:
            resp = requests.post(
                url,
                headers=headers,
                files={"file": (p.name, f, "application/json")},
                timeout=timeout,
            )
    except requests.RequestException as exc:
        logger.warning("[cpa] 上传请求失败 file=%s err=%s", p.name, exc)
        return False, f"网络异常: {exc}"

    if resp.status_code == 200:
        logger.info("[cpa] 已上传: %s", p.name)
        return True, f"已上传 {p.name}"
    body = (resp.text or "")[:200]
    logger.warning("[cpa] 上传失败 status=%d body=%s", resp.status_code, body)
    return False, f"CPA HTTP {resp.status_code}: {body}"
