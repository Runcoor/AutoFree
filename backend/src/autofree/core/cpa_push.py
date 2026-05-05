"""把 freegen bundle 推到 CPA — autofree 版,推前自动 refresh access_token。

逻辑:
  - storage.write_auth_json 已经写好 output/auth/<email>.json (注册当时的 token)
  - 推送时:先解 JWT exp 看 access_token 是否过期 (默认 skew 120s)
  - 过期 → autofree.core.oauth.refresh_access_token (1 次 HTTP) 换新对
  - 写回 JSON 文件 → 上传 CPA
  - refresh 失败 → warning + 仍尝试推 (让用户看到 401 而不是隐藏)

返:
  (ok, message)
  - ok=True: 已推 CPA / CPA 未启用故跳过
  - ok=False: 真正出错
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

from autofree.core.cpa_sync import is_cpa_configured, upload_to_cpa

logger = logging.getLogger(__name__)


# ─── token 过期检测 ─────────────────────────────────────────────────────

def _jwt_payload(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_token_expired(access_token: str, *, skew_seconds: int = 120) -> bool:
    payload = _jwt_payload(access_token)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return True  # 没 exp 字段保守视为过期
    return time.time() + skew_seconds >= exp


# ─── 刷新 + 回写 ─────────────────────────────────────────────────────────

def _utc_iso(ts: float) -> str:
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=8))
    return _dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def refresh_and_write(json_path: Path) -> tuple[bool, str, dict | None]:
    """读 JSON → refresh → 回写。成功返 (True, msg, new_bundle)。

    new_bundle 是写盘后的新数据(含 access_token / refresh_token / id_token / expires_at epoch)。
    """
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"JSON 解析失败: {exc}", None

    refresh_tok = raw.get("refresh_token", "")
    if not refresh_tok:
        return False, "缺 refresh_token,无法刷新", None

    from autofree.core.oauth import refresh_access_token
    result = refresh_access_token(refresh_tok)
    if not result or not result.get("access_token"):
        return False, "refresh 失败 (refresh_token 也可能死了 → 需重登)", None

    expires_at = time.time() + int(result.get("expires_in", 3600))
    raw.update({
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token") or refresh_tok,
        "id_token": result.get("id_token") or raw.get("id_token", ""),
        "expired": _utc_iso(expires_at),
        "last_refresh": _utc_iso(time.time()),
    })
    json_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    raw["expires_at"] = expires_at  # 给上层用,不写盘
    return True, f"已刷新 (下次过期 {int(expires_at - time.time())}s 后)", raw


# ─── 主入口 ─────────────────────────────────────────────────────────────

def push_auth_file(json_path: str | Path, *, refresh: bool = True, force_refresh: bool = False) -> tuple[bool, str]:
    """把已落盘的 auth JSON 推到 CPA(默认推前 refresh)。

    json_path: storage.write_auth_json 写出的 output/auth/<email>.json。
    refresh=True (默认): 检测到过期 (或 force_refresh=True) 才 refresh。
    refresh=False: 完全不动 JSON,直接推。
    """
    p = Path(json_path)
    if not p.exists():
        return False, f"文件不存在: {p}"

    if not is_cpa_configured():
        logger.info("[cpa_push] CPA 未启用,跳过 file=%s", p.name)
        return True, "CPA 未启用,已跳过"

    if refresh:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            access = raw.get("access_token", "")
        except Exception as exc:
            return False, f"JSON 读取失败: {exc}"

        if force_refresh or _is_token_expired(access):
            ok, msg, _ = refresh_and_write(p)
            if ok:
                logger.info("[cpa_push] %s refresh 成功,准备推送", p.name)
            else:
                logger.warning("[cpa_push] %s refresh 失败: %s — 仍尝试用旧 token 推", p.name, msg)

    return upload_to_cpa(p)
