"""账号 / pending 列表 + 下载 + 手动导入 + 批量重推 CPA(自动 refresh)。"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from autofree.db.base import get_db
from autofree.db.models import Account, PendingAccount
from autofree.deps import require_user
from autofree.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


def _serialize_account(a: Account) -> dict:
    return {
        "id": a.id,
        "batch_id": a.batch_id,
        "email": a.email,
        "password": a.password,
        "account_id": a.account_id,
        "plan_type": a.plan_type,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "auth_json_path": a.auth_json_path,
        "cpa_synced": a.cpa_synced,
        "cpa_synced_at": a.cpa_synced_at.isoformat() if a.cpa_synced_at else None,
        "cpa_error": a.cpa_error,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _serialize_pending(p: PendingAccount) -> dict:
    return {
        "id": p.id,
        "batch_id": p.batch_id,
        "email": p.email,
        "password": p.password,
        "error_kind": p.error_kind,
        "error": p.error,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "resolved_at": p.resolved_at.isoformat() if p.resolved_at else None,
        "resolved_via": p.resolved_via,
    }


@router.get("")
def list_accounts(
    page: int = 1,
    page_size: int = 50,
    batch_id: Optional[str] = None,
    cpa_synced: Optional[bool] = None,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, 500))

    stmt = select(Account)
    if batch_id:
        stmt = stmt.where(Account.batch_id == batch_id)
    if cpa_synced is not None:
        stmt = stmt.where(Account.cpa_synced.is_(cpa_synced))

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0

    rows = db.execute(
        stmt.order_by(desc(Account.created_at)).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [_serialize_account(a) for a in rows],
    }


@router.post("/cpa-reconcile")
def cpa_reconcile(
    db: Session = Depends(get_db), _user=Depends(require_user),
) -> dict:
    """跟 CPA 对账 — 拉 CPA 上的现有 auth-files,把本地状态对齐:

    - 本地 cpa_synced=True 但 CPA 上不存在 → 标记 cpa_synced=False + cpa_error="CPA 上已删除"
    - 本地 cpa_synced=True 且 CPA 存在但 status!=active 或 disabled=True → cpa_error 记 CPA 状态
    - 本地不存在但 CPA 上有 → 仅汇总数量返回,不自动 INSERT(那些大概率不是本 AutoFree 注册的)
    """
    from autofree.core.cpa_sync import list_cpa_inventory

    ok, payload = list_cpa_inventory()
    if not ok:
        raise HTTPException(502, f"拉 CPA 列表失败: {payload}")
    cpa_files = payload  # type: ignore[assignment]

    # 用 email 字段做主键(必要时回退到 account 字段)
    cpa_by_email: dict[str, dict] = {}
    for f in cpa_files:
        e = (f.get("email") or f.get("account") or "").strip()
        if e:
            cpa_by_email[e] = f

    local = db.execute(select(Account)).scalars().all()
    removed = []         # 本地认为同步成功但 CPA 上已删除
    status_issues = []   # CPA 上有但状态异常
    healthy = 0
    restored = 0         # 本地标了失败但 CPA 上其实有 → 修正回 synced

    for a in local:
        cpa_row = cpa_by_email.get(a.email)
        if cpa_row is None:
            if a.cpa_synced:
                a.cpa_synced = False
                a.cpa_error = "CPA 上已被删除"
                removed.append(a.email)
            continue
        # CPA 上确实有
        cpa_status = (cpa_row.get("status") or "").lower()
        cpa_disabled = bool(cpa_row.get("disabled"))
        cpa_unavailable = bool(cpa_row.get("unavailable"))
        if cpa_disabled or cpa_unavailable or (cpa_status and cpa_status != "active"):
            label = "disabled" if cpa_disabled else "unavailable" if cpa_unavailable else cpa_status
            a.cpa_synced = False
            a.cpa_error = f"CPA 状态:{label}"
            status_issues.append({"email": a.email, "status": label})
            continue
        # 健康
        if not a.cpa_synced:
            a.cpa_synced = True
            a.cpa_synced_at = _utcnow()
            a.cpa_error = None
            restored += 1
        healthy += 1

    db.commit()

    local_emails = {a.email for a in local}
    cpa_only = sum(1 for e in cpa_by_email if e not in local_emails)

    return {
        "cpa_total": len(cpa_files),
        "local_total": len(local),
        "cpa_only_count": cpa_only,  # CPA 上有但本地没有的(其它来源)
        "healthy": healthy,
        "restored": restored,        # 本地误标失败,CPA 其实正常 → 修回 synced
        "removed_on_cpa": removed,   # 本地以为成功,CPA 已删
        "status_issues": status_issues,
    }


@router.get("/cpa-stats")
def cpa_stats(
    db: Session = Depends(get_db), _user=Depends(require_user),
) -> dict:
    """CPA 同步概览 — 账号页顶部展示用。

    返回:
      total: 全部账号
      synced: cpa_synced=True
      failed: cpa_synced=False && cpa_error 非空(推送过但失败)
      unsynced: cpa_synced=False && cpa_error 为空(从未推过)
      sync_rate: 已同步占比 (0-1)
    """
    rows = db.execute(select(Account)).scalars().all()
    total = len(rows)
    synced = sum(1 for r in rows if r.cpa_synced)
    failed = sum(1 for r in rows if not r.cpa_synced and r.cpa_error)
    unsynced = total - synced - failed
    rate = (synced / total) if total else 0.0
    return {
        "total": total,
        "synced": synced,
        "failed": failed,
        "unsynced": unsynced,
        "sync_rate": round(rate, 4),
    }


# ─── CPA 全景:管理 CPA 上所有 auth-files ────────────────────────────────

@router.get("/cpa-inventory")
def cpa_inventory(
    db: Session = Depends(get_db), _user=Depends(require_user),
) -> dict:
    """列出 CPA 上所有 auth-files,标注每条是否在本地 AutoFree DB 里。

    返回 items + summary,前端用来做「CPA 全景」管理页。
    """
    from autofree.core.cpa_sync import list_cpa_inventory

    ok, payload = list_cpa_inventory()
    if not ok:
        raise HTTPException(502, f"拉 CPA 列表失败: {payload}")
    cpa_files = payload  # type: ignore[assignment]

    local_emails = {a.email for a in db.execute(select(Account)).scalars().all()}

    items = []
    n_active = n_disabled = n_unavailable = n_other = 0
    n_in_local = 0
    for f in cpa_files:
        email = (f.get("email") or f.get("account") or "").strip()
        status = (f.get("status") or "").lower()
        disabled = bool(f.get("disabled"))
        unavailable = bool(f.get("unavailable"))
        in_local = email in local_emails if email else False
        if disabled:
            n_disabled += 1
        elif unavailable:
            n_unavailable += 1
        elif status == "active":
            n_active += 1
        else:
            n_other += 1
        if in_local:
            n_in_local += 1
        items.append({
            "name": f.get("name") or f.get("id") or "",
            "id": f.get("id") or f.get("name") or "",
            "email": email,
            "type": f.get("type") or "",
            "status": status,
            "status_message": f.get("status_message") or "",
            "disabled": disabled,
            "unavailable": unavailable,
            "size": f.get("size"),
            "updated_at": f.get("updated_at"),
            "success": f.get("success"),
            "failed": f.get("failed"),
            "in_local": in_local,
            # 是否「失败状态」 — 给前端批量按钮用
            "is_failed_state": disabled or unavailable or (bool(status) and status != "active"),
        })

    # 按 (失败状态优先 / 不在本地优先 / email)
    items.sort(key=lambda x: (
        not x["is_failed_state"],
        x["in_local"],
        x["email"] or x["name"],
    ))

    return {
        "items": items,
        "summary": {
            "total": len(items),
            "active": n_active,
            "disabled": n_disabled,
            "unavailable": n_unavailable,
            "other_status": n_other,
            "in_local": n_in_local,
            "cpa_only": len(items) - n_in_local,
        },
    }


class CpaDeleteParams(BaseModel):
    names: list[str] = Field(..., min_length=1, max_length=500)


@router.post("/cpa-inventory/delete")
def cpa_inventory_delete(
    params: CpaDeleteParams,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    """从 CPA 删除一个或多个 auth-file(按文件名)。

    同时把本地 AutoFree DB 里同 email 的 Account.cpa_synced 标 False(若存在)。
    """
    from autofree.core.cpa_sync import delete_cpa_file, list_cpa_inventory

    # 先拉 inventory 用于 name → email 反查(便于回写本地状态)
    ok, payload = list_cpa_inventory()
    cpa_by_name: dict[str, dict] = {}
    if ok:
        for f in payload:  # type: ignore[union-attr]
            n = f.get("name") or f.get("id") or ""
            if n:
                cpa_by_name[n] = f

    results = []
    succeeded = 0
    failed = 0
    affected_local: list[str] = []

    for name in params.names:
        name = (name or "").strip()
        if not name:
            results.append({"name": name, "ok": False, "msg": "name 为空"})
            failed += 1
            continue
        ok2, msg = delete_cpa_file(name)
        results.append({"name": name, "ok": ok2, "msg": msg})
        if ok2:
            succeeded += 1
            # 同步本地状态
            email = (cpa_by_name.get(name) or {}).get("email") or ""
            email = email.strip()
            if email:
                row = db.execute(
                    select(Account).where(Account.email == email),
                ).scalar_one_or_none()
                if row and row.cpa_synced:
                    row.cpa_synced = False
                    row.cpa_error = "已从 CPA 删除"
                    affected_local.append(email)
        else:
            failed += 1

    if affected_local:
        db.commit()

    return {
        "total": len(params.names),
        "succeeded": succeeded,
        "failed": failed,
        "affected_local_count": len(affected_local),
        "affected_local_emails": affected_local,
        "results": results,
    }


@router.post("/sync-cpa/all-unsynced")
def sync_all_unsynced(
    force_refresh: bool = False,
    include_failed: bool = True,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    """一键推送所有未同步账号(可选包含之前推送失败的)。

    每个账号都先 refresh access_token 再推。返回汇总 + 每条结果。
    """
    from autofree.core.cpa_push import push_auth_file

    stmt = select(Account).where(Account.cpa_synced.is_(False))
    if not include_failed:
        # 只推从未尝试过的;之前推过但失败的不重试
        stmt = stmt.where((Account.cpa_error.is_(None)) | (Account.cpa_error == ""))
    rows = db.execute(stmt).scalars().all()

    settings = get_settings()
    pushed = 0
    failed = 0
    skipped = 0
    results: list[dict] = []
    for a in rows:
        if not a.auth_json_path:
            failed += 1
            results.append({"email": a.email, "ok": False, "msg": "无 auth_json_path"})
            continue
        p = settings.output_dir / a.auth_json_path
        if not p.exists():
            failed += 1
            results.append({"email": a.email, "ok": False, "msg": "本地 JSON 已不存在"})
            continue
        ok, msg = push_auth_file(p, refresh=True, force_refresh=force_refresh)
        results.append({"email": a.email, "ok": ok, "msg": msg})
        if "未启用" in msg:
            skipped += 1
        elif ok:
            pushed += 1
            a.cpa_synced = True
            a.cpa_synced_at = _utcnow()
            a.cpa_error = None
        else:
            failed += 1
            a.cpa_synced = False
            a.cpa_error = msg
    db.commit()
    return {
        "total": len(rows),
        "pushed": pushed,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


@router.post("/{email}/re-auth", status_code=202)
def reauth_account(
    email: str,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    """对一个已存在的 Account 重跑 OAuth(适合 refresh_token 失效的情况)。

    复用 freegen 的 resume runner — 浏览器登录 → 拿新 bundle → 写 auth.json → 推 CPA。
    成功后 Account 行的 token / cpa_synced 会被更新。
    """
    a = db.execute(select(Account).where(Account.email == email)).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "账号不存在")
    if not a.password:
        raise HTTPException(400, "该账号缺密码,无法重新登录")

    # 复用 resume 端点的实现 — 直接调函数体
    from autofree.api.freegen import _current, _lock, _runner_resume
    import threading
    import uuid

    with _lock:
        if _current and _current.get("stage") not in ("finished", "stopped", "failed"):
            raise HTTPException(409, "已有任务在运行,请等待结束或先 stop")

        task_id = uuid.uuid4().hex[:12]
        # 注意:不能直接赋 _current,得通过 freegen 模块的引用
        from autofree.api import freegen as _f
        _f._current = {
            "task_id": task_id,
            "batch_id": a.batch_id,
            "stage": "pending",
            "index": 0,
            "total": 1,
            "ok": 0,
            "failed": 0,
            "current_email": email,
            "events": [],
            "stop_requested": False,
            "thread": None,
            "started_at": None,
            "mode": "reauth",
        }
        from autofree.core.control import reset_stop
        reset_stop()

        t = threading.Thread(
            target=_runner_resume, args=(task_id, email, a.password),
            name=f"reauth-{task_id}", daemon=True,
        )
        _f._current["thread"] = t
        t.start()

    return {"task_id": task_id, "email": email, "batch_id": a.batch_id, "mode": "reauth"}


@router.get("/{email}/auth.json")
def download_auth(email: str, db: Session = Depends(get_db), _user=Depends(require_user)):
    a = db.execute(select(Account).where(Account.email == email)).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "账号不存在")
    settings = get_settings()
    p = (settings.output_dir / a.auth_json_path) if a.auth_json_path else None
    if not p or not p.exists():
        raise HTTPException(410, "本地 JSON 文件已不存在")
    return FileResponse(p, filename=f"codex-{email}-free.json", media_type="application/json")


# ─── pending ─────────────────────────────────────────────────────────────

@router.get("/pending")
def list_pending(
    db: Session = Depends(get_db), _user=Depends(require_user)
) -> dict:
    rows = db.execute(
        select(PendingAccount)
        .where(PendingAccount.resolved_at.is_(None))
        .order_by(desc(PendingAccount.created_at))
    ).scalars().all()
    return {"items": [_serialize_pending(p) for p in rows]}


@router.post("/pending/{email}/manual-import")
def manual_import(
    email: str,
    json_content: dict = Body(..., embed=False),
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    p = db.execute(
        select(PendingAccount).where(PendingAccount.email == email).order_by(desc(PendingAccount.created_at))
    ).scalars().first()
    if not p:
        raise HTTPException(404, "无该 pending 账号")

    settings = get_settings()
    target_dir = settings.output_dir / "manual_auth"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{email}.json"
    target.write_text(json.dumps(json_content, ensure_ascii=False, indent=2), encoding="utf-8")

    import datetime as _dt
    p.resolved_at = _dt.datetime.now(_dt.timezone.utc)
    p.resolved_via = "manual_import"
    db.commit()

    logger.info("[accounts] pending %s manual-import 完成 → %s", email, target)
    return {"ok": True, "path": str(target)}


@router.delete("/pending/{email}", status_code=204)
def delete_pending(
    email: str, db: Session = Depends(get_db), _user=Depends(require_user)
) -> None:
    rows = db.execute(select(PendingAccount).where(PendingAccount.email == email)).scalars().all()
    for r in rows:
        db.delete(r)
    db.commit()


# ─── 批次/单号 重推 CPA(推前自动 refresh)──────────────────────────────────

@router.post("/{email}/sync-cpa")
def sync_one(
    email: str,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    """单个账号重推 CPA — 自动 refresh access_token 后再推。"""
    from autofree.core.cpa_push import push_auth_file

    a = db.execute(select(Account).where(Account.email == email)).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "账号不存在")
    settings = get_settings()
    p = (settings.output_dir / a.auth_json_path) if a.auth_json_path else None
    if not p or not p.exists():
        raise HTTPException(410, "本地 JSON 文件已不存在")

    ok, msg = push_auth_file(p, refresh=True, force_refresh=force_refresh)
    a.cpa_synced = ok and "未启用" not in msg
    a.cpa_synced_at = _utcnow() if a.cpa_synced else a.cpa_synced_at
    a.cpa_error = None if ok else msg
    db.commit()
    return {"ok": ok, "msg": msg, "email": email}


@router.post("/batch/{batch_id}/sync-cpa")
def sync_batch(
    batch_id: str,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    """整批账号重推 CPA — 每个账号都自动 refresh 后推。"""
    from autofree.core.cpa_push import push_auth_file

    rows = db.execute(select(Account).where(Account.batch_id == batch_id)).scalars().all()
    if not rows:
        raise HTTPException(404, f"batch {batch_id} 没有账号")

    settings = get_settings()
    pushed = 0
    failed = 0
    skipped = 0
    results = []
    for a in rows:
        if not a.auth_json_path:
            failed += 1
            results.append({"email": a.email, "ok": False, "msg": "无 auth_json_path"})
            continue
        p = settings.output_dir / a.auth_json_path
        if not p.exists():
            failed += 1
            results.append({"email": a.email, "ok": False, "msg": "本地 JSON 已不存在"})
            continue
        ok, msg = push_auth_file(p, refresh=True, force_refresh=force_refresh)
        results.append({"email": a.email, "ok": ok, "msg": msg})
        if "未启用" in msg:
            skipped += 1
        elif ok:
            pushed += 1
            a.cpa_synced = True
            a.cpa_synced_at = _utcnow()
            a.cpa_error = None
        else:
            failed += 1
            a.cpa_synced = False
            a.cpa_error = msg
    db.commit()
    return {
        "batch_id": batch_id,
        "total": len(rows),
        "pushed": pushed,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }
