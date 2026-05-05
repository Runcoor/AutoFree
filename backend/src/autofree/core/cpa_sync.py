"""CPA (CLIProxyAPI) — 上传 + 只读对账。

- upload_to_cpa: 单文件上传到 CPA(POST /v0/management/auth-files)
- list_cpa_inventory: 拉 CPA 当前 auth-files 列表(只读对账用)

CPA url / key 从 DB Setting 表读,web 改了立即生效。
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


def delete_cpa_file(name: str, *, timeout: int = 10) -> tuple[bool, str]:
    """从 CPA 删一个 auth-file(按文件名)。

    name 是 list_cpa_inventory 返回的 file["name"](或 file["id"]),如 codex-xxx@foo.com-free.json。
    返 (ok, message)。404 当作"已经不在了"也返 True 方便上层批量删。
    """
    cfg = get_cpa_config()
    if not (cfg["url"] and cfg["key"]):
        return False, "CPA url/key 未配置"
    if not name:
        return False, "name 不能为空"

    url = cfg["url"].rstrip("/") + "/v0/management/auth-files"
    headers = {"Authorization": f"Bearer {cfg['key']}"}

    try:
        resp = requests.delete(url, headers=headers, params={"name": name}, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("[cpa] 删请求异常 name=%s err=%s", name, exc)
        return False, f"网络异常: {exc}"

    if resp.status_code in (200, 204):
        logger.info("[cpa] 已删: %s", name)
        return True, f"已删 {name}"
    if resp.status_code == 404:
        # 已经不在 = 成功(幂等)
        logger.info("[cpa] %s 已不在 CPA(404 视作删成功)", name)
        return True, f"{name} 已不在 CPA"
    body = (resp.text or "")[:200]
    logger.warning("[cpa] 删失败 name=%s status=%d body=%s", name, resp.status_code, body)
    return False, f"CPA HTTP {resp.status_code}: {body}"


def list_cpa_inventory(*, timeout: int = 10) -> tuple[bool, list[dict] | str]:
    """拉 CPA 上的当前 auth-files 列表(只读对账用)。

    成功:(True, [{email, status, disabled, ...}, ...])
    失败:(False, error_msg)
    """
    cfg = get_cpa_config()
    if not (cfg["url"] and cfg["key"]):
        return False, "CPA url/key 未配置"

    url = cfg["url"].rstrip("/") + "/v0/management/auth-files"
    headers = {"Authorization": f"Bearer {cfg['key']}"}

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"网络异常: {exc}"

    if resp.status_code != 200:
        return False, f"CPA HTTP {resp.status_code}: {(resp.text or '')[:200]}"

    try:
        data = resp.json()
    except Exception as exc:
        return False, f"CPA 响应非 JSON: {exc}"

    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return False, f"CPA 响应缺 files 字段: {data!r}"
    return True, files
