"""freegen 注册批次 API — start / stop / status。

并发模型:全局单 batch(沿用 freegen 的 _playwright_lock 思路)。
重新启动 batch 必须先等当前结束或主动 stop。

Domain 选择策略:start 入参 domain 可选 — 不传则按 round-robin 选 enabled 域名。
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autofree.db.base import SessionLocal, get_db
from autofree.db.models import Account, Batch, Domain, PendingAccount
from autofree.deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── 全局任务状态(进程内,简单串行)────────────────────────────────────────

_lock = threading.Lock()
_current: dict[str, Any] | None = None  # {"task_id", "thread", "stop_requested", "events", ...}


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


# ─── 域名选择 ────────────────────────────────────────────────────────────

def _pick_domain(db: Session) -> str:
    """round-robin:从 enabled 域名里选 last_used_at 最早的(NULL 视为最早)。"""
    enabled = db.execute(
        select(Domain).where(Domain.enabled.is_(True))
    ).scalars().all()
    if not enabled:
        raise HTTPException(400, "域名池为空 — 请先在设置里添加 cloud-mail 域名并启用")
    # NULL last_used_at 排在最前
    enabled.sort(key=lambda d: (d.last_used_at is not None, d.last_used_at or _dt.datetime.min))
    return enabled[0].domain


def _persist_account(info: dict, *, domain: str) -> None:
    """把 batch 的 account_done 事件落 DB:
    - ok=True → INSERT Account + 同时 bump domain success
    - ok=False && register_done → INSERT PendingAccount + bump domain fail
    - ok=False && !register_done → 仅 bump domain fail(邮箱已 drop,无需 pending)
    """
    email = (info.get("email") or "").strip()
    if not email:
        return
    ok = bool(info.get("ok"))

    with SessionLocal() as db:
        if ok:
            existing = db.execute(select(Account).where(Account.email == email)).scalar_one_or_none()
            if existing:
                logger.warning("[persist] account 已存在 email=%s 跳过", email)
            else:
                expires_dt = None
                exp = info.get("expires_at")
                if isinstance(exp, (int, float)):
                    expires_dt = _dt.datetime.fromtimestamp(exp, _dt.timezone.utc)
                # auth_json_path 存相对 output_dir 的路径,方便 download 端点拼回去
                from autofree.settings import get_settings
                output_dir = get_settings().output_dir
                full = info.get("auth_json_path", "")
                rel = ""
                if full:
                    try:
                        from pathlib import Path as _Path
                        rel = str(_Path(full).resolve().relative_to(_Path(output_dir).resolve()))
                    except Exception:
                        rel = info.get("auth_file", "")
                a = Account(
                    batch_id=info.get("batch_id", ""),
                    email=email,
                    password=info.get("password", ""),
                    account_id=info.get("account_id", ""),
                    plan_type=info.get("plan_type", "free") or "free",
                    access_token=info.get("access_token", ""),
                    refresh_token=info.get("refresh_token", ""),
                    id_token=info.get("id_token", ""),
                    expires_at=expires_dt,
                    last_refresh=_utcnow(),
                    auth_json_path=rel,
                    cpa_synced=bool(info.get("cpa_pushed")),
                    cpa_synced_at=_utcnow() if info.get("cpa_pushed") else None,
                    cpa_error=None if info.get("cpa_pushed") else (info.get("cpa_msg") or None),
                )
                db.add(a)
        else:
            if info.get("register_done"):
                p = PendingAccount(
                    batch_id=info.get("batch_id", ""),
                    email=email,
                    password=info.get("password", ""),
                    error_kind=info.get("error_kind", ""),
                    error=info.get("error", ""),
                )
                db.add(p)
        _bump_domain_stat(db, domain, ok)


def _bump_domain_stat(db: Session, domain: str, ok: bool) -> None:
    row = db.execute(select(Domain).where(Domain.domain == domain)).scalar_one_or_none()
    if not row:
        return
    if ok:
        row.success_count += 1
    else:
        row.fail_count += 1
    row.last_used_at = _utcnow()
    db.commit()


# ─── Pydantic ──────────────────────────────────────────────────────────

class StartParams(BaseModel):
    count: int = Field(default=1, ge=1, le=200)
    domain: Optional[str] = None

    @field_validator("domain")
    @classmethod
    def _strip(cls, v):
        if v is None:
            return None
        v = v.strip().lstrip("@").lower()
        return v or None


# ─── 状态序列化 ──────────────────────────────────────────────────────────

def _serialize_batch(b: Batch) -> dict:
    return {
        "id": b.id,
        "domain": b.domain,
        "count": b.count,
        "status": b.status,
        "started_at": b.started_at.isoformat() if b.started_at else None,
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
        "ok": b.ok,
        "failed": b.failed,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


def _snapshot_running() -> dict:
    """当前 in-progress 任务的快照(无任务返 {})。"""
    if not _current:
        return {}
    return {
        "task_id": _current["task_id"],
        "batch_id": _current.get("batch_id"),
        "stage": _current.get("stage", "pending"),
        "index": _current.get("index", 0),
        "total": _current.get("total", 0),
        "ok": _current.get("ok", 0),
        "failed": _current.get("failed", 0),
        "current_email": _current.get("current_email", ""),
        "started_at": _current.get("started_at"),
        "events": list(_current.get("events", []))[-50:],
    }


# ─── 核心 runner — 后台 thread ───────────────────────────────────────────

def _runner(task_id: str, count: int, domain: str) -> None:
    from autofree.core.batch import run_batch

    state = _current
    assert state is not None and state["task_id"] == task_id

    state["stage"] = "starting"
    state["started_at"] = time.time()

    # 落 Batch 行
    with SessionLocal() as db:
        b = Batch(id=state["batch_id"], domain=domain, count=count, status="running",
                  started_at=_utcnow())
        db.add(b)
        db.commit()

    # progress callback,每个 stage 都落 events + 写 DB(account/pending)
    def _cb(stage: str, info: dict):
        if state.get("stop_requested"):
            from autofree.core.control import request_stop
            request_stop()

        state["stage"] = stage
        ev = {"ts": time.time(), "stage": stage, **info}
        state.setdefault("events", []).append(ev)
        if len(state["events"]) > 200:
            del state["events"][: len(state["events"]) - 200]

        if stage == "account_started":
            state["index"] = info.get("index", state.get("index", 0))
            state["current_email"] = info.get("email") or ""
        elif stage == "account_done":
            state["index"] = info.get("index", state.get("index", 0))
            email = info.get("email") or state.get("current_email", "")
            ok = bool(info.get("ok"))
            if ok:
                state["ok"] = state.get("ok", 0) + 1
            else:
                state["failed"] = state.get("failed", 0) + 1
            _persist_account(info, domain=domain)

    error_msg: str | None = None
    try:
        result = run_batch(count=count, domain=domain, progress_cb=_cb)
        state["result"] = result
    except Exception as exc:
        logger.exception("[freegen] runner 异常")
        error_msg = str(exc)
        state["error"] = error_msg
    finally:
        # 完成 → DB Batch 落 finished
        finished = "finished"
        if state.get("stop_requested"):
            finished = "stopped"
        if error_msg:
            finished = "failed"
        with SessionLocal() as db:
            b = db.get(Batch, state["batch_id"])
            if b:
                b.status = finished
                b.ok = state.get("ok", 0)
                b.failed = state.get("failed", 0)
                b.finished_at = _utcnow()
                db.commit()
        state["stage"] = finished
        state["events"].append({"ts": time.time(), "stage": "task_ended", "status": finished})


# ─── 路由 ────────────────────────────────────────────────────────────

@router.post("/start", status_code=202)
def start(
    params: StartParams,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    global _current
    with _lock:
        if _current and _current.get("stage") not in ("finished", "stopped", "failed"):
            raise HTTPException(409, "已有任务在运行,请等待结束或先 stop")

        domain = params.domain or _pick_domain(db)

        task_id = uuid.uuid4().hex[:12]
        batch_id = uuid.uuid4().hex[:12]
        _current = {
            "task_id": task_id,
            "batch_id": batch_id,
            "stage": "pending",
            "index": 0,
            "total": params.count,
            "ok": 0,
            "failed": 0,
            "current_email": "",
            "events": [],
            "stop_requested": False,
            "thread": None,
            "started_at": None,
        }

        # 清掉历史 stop 信号(参考 autoteam _register_freegen_task 的 reset_stop)
        from autofree.core.control import reset_stop
        reset_stop()

        t = threading.Thread(target=_runner, args=(task_id, params.count, domain),
                             name=f"freegen-{task_id}", daemon=True)
        _current["thread"] = t
        t.start()

    return {"task_id": task_id, "batch_id": batch_id, "domain": domain, "count": params.count}


@router.post("/stop")
def stop(_user=Depends(require_user)) -> dict:
    if not _current or _current.get("stage") in ("finished", "stopped", "failed"):
        raise HTTPException(404, "无运行中的任务")
    _current["stop_requested"] = True
    from autofree.core.control import request_stop
    request_stop()
    return {"ok": True, "msg": "stop 已请求,任务将在当前账号结束后停止"}


@router.get("/status")
def status(_user=Depends(require_user)) -> dict:
    return _snapshot_running()


@router.get("/batches")
def batches(
    limit: int = 20,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    rows = db.execute(
        select(Batch).order_by(Batch.created_at.desc()).limit(min(limit, 200))
    ).scalars().all()
    return {"items": [_serialize_batch(b) for b in rows]}
