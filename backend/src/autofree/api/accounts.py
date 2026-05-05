"""账号 / pending 列表 + 下载 + 手动导入。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from autofree.db.base import get_db
from autofree.db.models import Account, PendingAccount
from autofree.deps import require_user
from autofree.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


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
