"""CPA (CLIProxyAPI) — 上传 + 只读对账 + OAuth 补绑。

- upload_to_cpa: 单文件上传到 CPA(POST /v0/management/auth-files)
- list_cpa_inventory: 拉 CPA 当前 auth-files 列表(只读对账用)
- get_codex_auth_url / submit_oauth_callback: 帮 CPA 跑 Codex OAuth 流程,
  AutoFree 驱动浏览器登录 + 绑邮箱,把 callback URL 回填给 CPA

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


def get_codex_auth_url(*, timeout: int = 10) -> tuple[bool, dict | str]:
    """跟 CPA 要一个 Codex authorize URL(CPA 持 PKCE code_verifier,我们只拿 URL)。

    成功:(True, {"state": "xxx", "url": "https://auth.openai.com/oauth/authorize?..."})
    失败:(False, error_msg)

    CPA 端点:GET /v0/management/codex-auth-url?is_webui=true
    """
    cfg = get_cpa_config()
    if not (cfg["url"] and cfg["key"]):
        return False, "CPA url/key 未配置"

    url = cfg["url"].rstrip("/") + "/v0/management/codex-auth-url"
    headers = {"Authorization": f"Bearer {cfg['key']}"}
    try:
        resp = requests.get(url, headers=headers, params={"is_webui": "true"}, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"网络异常: {exc}"

    if resp.status_code != 200:
        return False, f"CPA HTTP {resp.status_code}: {(resp.text or '')[:200]}"

    try:
        data = resp.json()
    except Exception as exc:
        return False, f"CPA 响应非 JSON: {exc}"

    state = (data.get("state") or "").strip()
    auth_url = (data.get("url") or "").strip()
    if not state or not auth_url:
        return False, f"CPA 响应缺 state/url: {data!r}"
    return True, {"state": state, "url": auth_url}


def submit_oauth_callback(redirect_url: str, *, provider: str = "codex", timeout: int = 15) -> tuple[bool, str]:
    """把 OAuth callback URL 回填给 CPA — CPA 用它自己持有的 code_verifier 换 token。

    redirect_url 是浏览器最终落地的 http://localhost:1455/auth/callback?code=...&state=...
    必须含 code + state(CPA 会校验 state 防 CSRF + 用 verifier+code 换 token)。

    CPA 端点:POST /v0/management/oauth-callback
    body: { "provider": "codex", "redirect_url": "<完整 URL>" }
    """
    cfg = get_cpa_config()
    if not (cfg["url"] and cfg["key"]):
        return False, "CPA url/key 未配置"
    if not redirect_url:
        return False, "redirect_url 为空"

    url = cfg["url"].rstrip("/") + "/v0/management/oauth-callback"
    headers = {
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
    }
    body = {"provider": provider, "redirect_url": redirect_url}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"网络异常: {exc}"

    if resp.status_code in (200, 201, 202, 204):
        logger.info("[cpa] OAuth callback 已回填(provider=%s)", provider)
        return True, f"已回填 (HTTP {resp.status_code})"
    body_text = (resp.text or "")[:200]
    logger.warning("[cpa] OAuth callback 回填失败 status=%d body=%s", resp.status_code, body_text)
    return False, f"CPA HTTP {resp.status_code}: {body_text}"


def get_codex_auth_status(state: str, *, timeout: int = 5) -> tuple[bool, dict | str]:
    """轮询 CPA 看 OAuth 是否完成(诊断用,正常流程不需要 — 我们直接回填 callback)。

    成功:(True, {"status": "wait"|"ok"|"error", ...})
    """
    cfg = get_cpa_config()
    if not (cfg["url"] and cfg["key"]):
        return False, "CPA url/key 未配置"
    if not state:
        return False, "state 为空"

    url = cfg["url"].rstrip("/") + "/v0/management/get-auth-status"
    headers = {"Authorization": f"Bearer {cfg['key']}"}
    try:
        resp = requests.get(url, headers=headers, params={"state": state}, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"网络异常: {exc}"
    if resp.status_code != 200:
        return False, f"CPA HTTP {resp.status_code}: {(resp.text or '')[:200]}"
    try:
        return True, resp.json()
    except Exception as exc:
        return False, f"CPA 响应非 JSON: {exc}"
