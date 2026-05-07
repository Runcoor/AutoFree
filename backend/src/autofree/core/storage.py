"""freegen 输出存储。

每个号产出 2 件:
- 追加一行到 `accounts.txt`: `email|password|account_id|plan_type|created_at`
- 单独写一个 `auth/<email>.json`,字段对齐 CPA-importable 样本(顶层 access_token /
  refresh_token / id_token / account_id / email / expired / last_refresh / type / disabled)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from autofree.core.config import OUTPUT_DIR

logger = logging.getLogger(__name__)


def _utc_iso(ts: float) -> str:
    """秒级 epoch → CPA 样本风格的 ISO8601(+08:00,带时区偏移)。"""
    # 用 +08:00 与样本一致;CPA 解析时区时不会因为 Z 还是 +08 出问题
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=8))
    return _dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def write_auth_json(bundle: dict, *, output_dir: Path | None = None) -> Path:
    """把 OAuth bundle 写成 CPA 样本风格的 JSON。

    bundle 必填字段:
      access_token, refresh_token, id_token, account_id, email, expires_at(epoch sec)
    """
    out_dir = (output_dir or OUTPUT_DIR) / "auth"
    out_dir.mkdir(parents=True, exist_ok=True)
    email = bundle["email"]
    safe_email = email.replace("/", "_")
    path = out_dir / f"{safe_email}.json"

    auth_data = {
        "access_token": bundle["access_token"],
        "account_id": bundle["account_id"],
        "disabled": False,
        "email": email,
        "expired": _utc_iso(bundle["expires_at"]),
        "id_token": bundle["id_token"],
        "last_refresh": _utc_iso(time.time()),
        "refresh_token": bundle["refresh_token"],
        "type": "codex",
    }
    path.write_text(json.dumps(auth_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[storage] 写入 auth: %s", path)
    return path


def append_account_line(*, email: str, password: str | None, account_id: str, plan_type: str, output_dir: Path | None = None) -> Path:
    """追加 1 行到 accounts.txt(用 `|` 分隔)。password=None 写空串(email-only 登录场景)。"""
    out_dir = output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "accounts.txt"
    line = "|".join([email, password or "", account_id or "", plan_type or "", _utc_iso(time.time())])
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    logger.info("[storage] 追加账号: %s (%s)", email, plan_type)
    return path


# ─── pending 账号:注册成功但 OAuth 失败,等用户手动认证 ───────────────────────
PENDING_FILE = OUTPUT_DIR / "pending_accounts.jsonl"
MANUAL_AUTH_DIR = OUTPUT_DIR / "manual_auth"


def append_pending_account(
    *,
    email: str,
    password: str,
    batch_id: str = "",
    error_kind: str = "",
    error: str = "",
    phone_verified: bool = False,
) -> Path:
    """注册成功但 OAuth 失败的号写到 pending 列表(全局,非 per-batch)。

    之后用户可以:
      a) 在「待办」页点「继续验证」让系统重跑 OAuth + phone gate
      b) 在 UI 直接上传 JSON 内容,写到 manual_auth/<email>.json
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "email": email,
        "password": password,
        "batch_id": batch_id,
        "error_kind": error_kind,
        "error": error,
        "phone_verified": bool(phone_verified),
        "created_at": _utc_iso(time.time()),
    }
    with PENDING_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("[storage] pending 账号已记录: %s (kind=%s)", email, error_kind)
    return PENDING_FILE


def list_pending_accounts() -> list[dict]:
    """读 pending_accounts.jsonl,返回 dict 列表(去重 — 同 email 取最新一条)。"""
    if not PENDING_FILE.exists():
        return []
    by_email: dict[str, dict] = {}
    for line in PENDING_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            email = (r.get("email") or "").strip()
            if email:
                by_email[email] = r
        except Exception:
            continue
    return list(by_email.values())


def remove_pending_account(email: str) -> bool:
    """从 pending 列表删除(用户已经手动跑通 / 主动放弃)。返回是否真的删了。"""
    return remove_pending_accounts([email]) > 0


def remove_pending_accounts(emails) -> int:
    """批量删除。返回实际删除数量(可能 < len(emails),如果有不存在的)。

    一次写盘,效率比 N 次 remove_pending_account 高很多。
    """
    if not PENDING_FILE.exists():
        return 0
    targets = {(e or "").strip().lower() for e in emails if e}
    if not targets:
        return 0
    kept = []
    removed = 0
    for line in PENDING_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if (r.get("email") or "").strip().lower() in targets:
                removed += 1
                continue
        except Exception:
            pass
        kept.append(line)
    if removed:
        PENDING_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        logger.info("[storage] 从 pending 批量删除 %d 条 (请求 %d)", removed, len(targets))
    return removed


def import_manual_auth(email: str, json_content: str | dict) -> Path:
    """用户从外部拿到的 CPA-importable JSON,导入到 manual_auth/<email>.json。

    json_content 接受字符串(直接写)或 dict(序列化后写)。
    """
    MANUAL_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    safe_email = email.replace("/", "_")
    path = MANUAL_AUTH_DIR / f"{safe_email}.json"
    if isinstance(json_content, dict):
        path.write_text(json.dumps(json_content, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        # 校验是合法 JSON
        try:
            parsed = json.loads(json_content)
            path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            raise ValueError(f"json_content 不是合法 JSON: {exc}") from exc
    logger.info("[storage] 手动认证 JSON 已导入: %s", path)
    return path
