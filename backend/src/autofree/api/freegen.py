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
from autofree.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── 全局任务状态(进程内,简单串行)────────────────────────────────────────

_lock = threading.Lock()
_current: dict[str, Any] | None = None  # {"task_id", "thread", "stop_requested", "events", ...}


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


# ─── 域名选择 ────────────────────────────────────────────────────────────

def _enabled_domains(db: Session) -> list[str]:
    rows = db.execute(select(Domain).where(Domain.enabled.is_(True))).scalars().all()
    if not rows:
        raise HTTPException(400, "域名池为空 — 请先在设置里添加 cloud-mail 域名并启用")
    return [r.domain for r in rows]


def _pick_domain(db: Session) -> str:
    """round-robin:从 enabled 域名里选 last_used_at 最早的(NULL 视为最早)。
    用于「自动轮询」模式 —— 整批用同一个,下批换下一个。
    """
    enabled = db.execute(
        select(Domain).where(Domain.enabled.is_(True))
    ).scalars().all()
    if not enabled:
        raise HTTPException(400, "域名池为空 — 请先在设置里添加 cloud-mail 域名并启用")
    # NULL last_used_at 排在最前
    enabled.sort(key=lambda d: (d.last_used_at is not None, d.last_used_at or _dt.datetime.min))
    return enabled[0].domain


def _persist_account(info: dict, *, domain: str | None = None) -> None:
    """把 batch 的 account_done 事件落 DB:
    - ok=True → INSERT Account + 同时 bump domain success
    - ok=False && register_done → INSERT PendingAccount + bump domain fail
    - ok=False && !register_done → 仅 bump domain fail(邮箱已 drop,无需 pending)

    resume 模式(mode='resume'):
    - ok=True → INSERT Account + 把现有 PendingAccount 标 resolved
    - ok=False → 更新现有 PendingAccount 的 error / error_kind(不新增行)

    domain 参数为该账号实际使用的域名;随机模式下每号不同,所以优先用 info["domain"],
    其次从 email 反推,最后才回落到外部传入的 domain。
    """
    email = (info.get("email") or "").strip()
    if not email:
        return
    ok = bool(info.get("ok"))
    mode = info.get("mode")
    is_resume = mode == "resume"
    is_reauth = mode == "reauth"

    # 解析当前账号实际用的域名(随机模式下每号可能不同)
    actual_domain = (info.get("domain") or "").strip().lstrip("@")
    if not actual_domain and "@" in email:
        actual_domain = email.split("@", 1)[1].strip().lower()
    if not actual_domain:
        actual_domain = (domain or "").strip().lstrip("@")

    with SessionLocal() as db:
        if ok:
            existing = db.execute(select(Account).where(Account.email == email)).scalar_one_or_none()
            if existing and is_reauth:
                # reauth 模式 → 用新 bundle 覆盖老 token / 路径 / CPA 状态
                from pathlib import Path as _Path
                from autofree.settings import get_settings as _get_settings
                output_dir = _get_settings().output_dir
                full = info.get("auth_json_path", "")
                rel = ""
                if full:
                    try:
                        rel = str(_Path(full).resolve().relative_to(_Path(output_dir).resolve()))
                    except Exception:
                        rel = info.get("auth_file", "")
                exp = info.get("expires_at")
                expires_dt = None
                if isinstance(exp, (int, float)):
                    expires_dt = _dt.datetime.fromtimestamp(exp, _dt.timezone.utc)
                existing.access_token = info.get("access_token", "") or existing.access_token
                existing.refresh_token = info.get("refresh_token", "") or existing.refresh_token
                existing.id_token = info.get("id_token", "") or existing.id_token
                existing.expires_at = expires_dt or existing.expires_at
                existing.last_refresh = _utcnow()
                if rel:
                    existing.auth_json_path = rel
                existing.cpa_synced = bool(info.get("cpa_pushed"))
                existing.cpa_synced_at = _utcnow() if info.get("cpa_pushed") else existing.cpa_synced_at
                existing.cpa_error = None if info.get("cpa_pushed") else (info.get("cpa_msg") or None)
                logger.info("[persist] reauth: updated Account %s tokens + cpa_synced=%s",
                            email, existing.cpa_synced)
            elif existing:
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
                    password=info.get("password") or "",
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
            if is_resume:
                # 失败 → 更新现有 pending,而非新增重复行
                pending_row = db.execute(
                    select(PendingAccount).where(
                        PendingAccount.email == email,
                        PendingAccount.resolved_at.is_(None),
                    )
                ).scalars().first()
                if pending_row:
                    pending_row.error_kind = info.get("error_kind", "") or pending_row.error_kind
                    pending_row.error = info.get("error", "") or pending_row.error
            elif is_reauth:
                # reauth 失败 → 在现有 Account 上记 cpa_error(其他 token 不动)
                existing = db.execute(select(Account).where(Account.email == email)).scalar_one_or_none()
                if existing:
                    existing.cpa_synced = False
                    err_kind = info.get("error_kind") or ""
                    raw_err = info.get("error") or "reauth 失败"
                    # account_deactivated 等终结错误 → 加显眼前缀,UI 据此显示「已废」徽标
                    if err_kind == "deactivated":
                        existing.cpa_error = f"🪦 deactivated: {raw_err}"
                    else:
                        existing.cpa_error = raw_err
            elif info.get("register_done"):
                p = PendingAccount(
                    batch_id=info.get("batch_id", ""),
                    email=email,
                    password=info.get("password") or "",
                    error_kind=info.get("error_kind", ""),
                    error=info.get("error", ""),
                )
                db.add(p)

        # 任何成功路径(register / resume / manual-add / reauth)都把同 email 未解决的 pending 标 resolved
        if ok:
            pending_rows = db.execute(
                select(PendingAccount).where(
                    PendingAccount.email == email,
                    PendingAccount.resolved_at.is_(None),
                )
            ).scalars().all()
            for pr in pending_rows:
                pr.resolved_at = _utcnow()
                pr.resolved_via = mode or "manual_add"

        # 显式 commit — 不依赖 _bump_domain_stat 顺手提交(domain 行不存在时它会 early-return)
        db.commit()
        if actual_domain:
            _bump_domain_stat(db, actual_domain, ok)


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
    # 'fixed'  → 用 params.domain
    # 'rotate' → round-robin 选 enabled 域名(整批共用一个,下批换下一个) — 默认
    # 'random' → 每个号从 enabled 域名池里随机抽
    domain_mode: str = Field(default="rotate")

    @field_validator("domain")
    @classmethod
    def _strip(cls, v):
        if v is None:
            return None
        v = v.strip().lstrip("@").lower()
        return v or None

    @field_validator("domain_mode")
    @classmethod
    def _check_mode(cls, v):
        v = (v or "rotate").strip().lower()
        if v not in ("fixed", "rotate", "random"):
            raise ValueError("domain_mode 必须是 fixed/rotate/random 之一")
        return v


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

def _runner_resume(task_id: str, email: str, password: str | None, mode: str = "resume") -> None:
    """单号 resume / reauth runner — 重跑 OAuth(已验证号通常无需 phone gate)。

    mode='resume':失败号续验,成功 INSERT Account + 标 pending resolved
    mode='reauth':已存在 Account 用新 bundle 覆盖老 token / cpa 状态
    """
    from autofree.core.batch import run_single_resume

    state = _current
    assert state is not None and state["task_id"] == task_id

    state["stage"] = "starting"
    state["started_at"] = time.time()
    state["mode"] = mode

    domain = email.split("@", 1)[-1] if "@" in email else ""

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
            state["index"] = 1
            state["current_email"] = info.get("email") or email
        elif stage == "account_done":
            ok = bool(info.get("ok"))
            if ok:
                state["ok"] = 1
            else:
                state["failed"] = 1
            # 把外层 mode 注进 info,_persist_account 据此走对应分支
            info_with_mode = {**info, "mode": mode}
            _persist_account(info_with_mode, domain=domain)

    error_msg: str | None = None
    try:
        run_single_resume(
            email=email, password=password, batch_id=state["batch_id"], progress_cb=_cb,
        )
    except Exception as exc:
        logger.exception("[resume] runner 异常")
        error_msg = str(exc)
        state["error"] = error_msg
    finally:
        finished = "finished"
        if state.get("stop_requested"):
            finished = "stopped"
        if error_msg:
            finished = "failed"
        state["stage"] = finished
        state["events"].append({"ts": time.time(), "stage": "task_ended", "status": finished})


def _runner_resume_all(
    task_id: str,
    items: list[tuple[str, str | None, str]],
    mode: str = "resume",
) -> None:
    """串行 resume / reauth 多个账号 — items: list of (email, password, batch_id)。

    任一号失败不中断后续。Stop 请求会在当前号结束后生效。
    mode 决定 _persist_account 走 resume(写 pending)还是 reauth(更新 Account)分支。
    """
    from autofree.core.batch import run_single_resume
    from autofree.core.control import request_stop, reset_stop

    state = _current
    assert state is not None and state["task_id"] == task_id

    state["stage"] = "starting"
    state["started_at"] = time.time()
    state["mode"] = "reauth_all" if mode == "reauth" else "resume_all"

    error_msg: str | None = None
    try:
        for idx, (email, password, batch_id) in enumerate(items, start=1):
            if state.get("stop_requested"):
                logger.warning("[resume-all] 收到 stop — 第 %d 号前中断", idx)
                break

            domain = email.split("@", 1)[-1] if "@" in email else ""
            state["index"] = idx
            state["current_email"] = email

            # 每个号 reset_stop,这样上一号 BatchStopped 不会污染下一号
            reset_stop()

            def _cb(stage: str, info: dict, _email=email, _domain=domain, _idx=idx):
                if state.get("stop_requested"):
                    request_stop()

                # 把每号的 index 透出来,避免被 run_single_resume 内部的 1/1 覆盖
                state["stage"] = stage
                ev = {"ts": time.time(), "stage": stage, **info, "outer_index": _idx,
                      "outer_total": len(items)}
                state.setdefault("events", []).append(ev)
                if len(state["events"]) > 200:
                    del state["events"][: len(state["events"]) - 200]

                if stage == "account_started":
                    state["current_email"] = info.get("email") or _email
                elif stage == "account_done":
                    ok = bool(info.get("ok"))
                    if ok:
                        state["ok"] = state.get("ok", 0) + 1
                    else:
                        state["failed"] = state.get("failed", 0) + 1
                    info_with_mode = {**info, "mode": mode}
                    _persist_account(info_with_mode, domain=_domain)

            try:
                run_single_resume(
                    email=email, password=password, batch_id=batch_id, progress_cb=_cb,
                )
            except Exception:
                logger.exception("[resume-all] 第 %d 号(%s)异常,继续下一号", idx, email)
                state["failed"] = state.get("failed", 0) + 1

    except Exception as exc:
        logger.exception("[resume-all] runner 异常")
        error_msg = str(exc)
        state["error"] = error_msg
    finally:
        finished = "finished"
        if state.get("stop_requested"):
            finished = "stopped"
        if error_msg:
            finished = "failed"
        state["stage"] = finished
        state["current_email"] = ""
        state["events"].append({"ts": time.time(), "stage": "task_ended", "status": finished})


def _runner_manual_batch(task_id: str, items: list[tuple[str, str | None]]) -> None:
    """串行处理「手动添加账号」 — 用 email+password 走 magic-link OAuth,成功 → Account + CPA push。

    复用 run_single_resume(它跳过注册,直接登录拿 codex bundle)。
    任一号失败不影响后续;失败 → 写 PendingAccount(可在「待办」页继续验证)。
    """
    from autofree.core.batch import run_single_resume
    from autofree.core.control import request_stop, reset_stop

    state = _current
    assert state is not None and state["task_id"] == task_id

    state["stage"] = "starting"
    state["started_at"] = time.time()
    state["mode"] = "manual_add"
    batch_id = state["batch_id"]

    # 落 Batch 行(domain=manual,数量 = items 总数)
    with SessionLocal() as db:
        b = Batch(id=batch_id, domain="manual", count=len(items), status="running",
                  started_at=_utcnow())
        db.add(b)
        db.commit()

    error_msg: str | None = None
    try:
        for idx, (email, password) in enumerate(items, start=1):
            if state.get("stop_requested"):
                logger.warning("[manual-add] 收到 stop — 第 %d 号前中断", idx)
                break

            state["index"] = idx
            state["current_email"] = email
            domain = email.split("@", 1)[-1] if "@" in email else ""

            reset_stop()

            def _cb(stage: str, info: dict, _email=email, _domain=domain, _idx=idx):
                if state.get("stop_requested"):
                    request_stop()

                state["stage"] = stage
                ev = {"ts": time.time(), "stage": stage, **info, "outer_index": _idx,
                      "outer_total": len(items)}
                state.setdefault("events", []).append(ev)
                if len(state["events"]) > 200:
                    del state["events"][: len(state["events"]) - 200]

                if stage == "account_started":
                    state["current_email"] = info.get("email") or _email
                elif stage == "account_done":
                    ok = bool(info.get("ok"))
                    if ok:
                        state["ok"] = state.get("ok", 0) + 1
                    else:
                        state["failed"] = state.get("failed", 0) + 1
                    # 关键:剥掉 run_single_resume 塞进来的 mode='resume',让 _persist_account 走默认分支
                    # ok=True → INSERT Account + 自动推 CPA(run_single_resume 内部已做)
                    # ok=False && register_done=True → INSERT PendingAccount(进「待办」)
                    info_clean = {k: v for k, v in info.items() if k != "mode"}
                    info_clean["batch_id"] = batch_id
                    _persist_account(info_clean, domain=_domain)

            try:
                run_single_resume(
                    email=email, password=password, batch_id=batch_id, progress_cb=_cb,
                )
            except Exception:
                logger.exception("[manual-add] 第 %d 号(%s)异常,继续下一号", idx, email)
                state["failed"] = state.get("failed", 0) + 1

    except Exception as exc:
        logger.exception("[manual-add] runner 异常")
        error_msg = str(exc)
        state["error"] = error_msg
    finally:
        finished = "finished"
        if state.get("stop_requested"):
            finished = "stopped"
        if error_msg:
            finished = "failed"
        # 更新 Batch 行
        with SessionLocal() as db:
            b = db.get(Batch, batch_id)
            if b:
                b.status = finished
                b.ok = state.get("ok", 0)
                b.failed = state.get("failed", 0)
                b.finished_at = _utcnow()
                db.commit()
        state["stage"] = finished
        state["current_email"] = ""
        state["events"].append({"ts": time.time(), "stage": "task_ended", "status": finished})


def _runner(task_id: str, count: int, domain: str | None, random_pool: list[str] | None) -> None:
    from autofree.core.batch import run_batch

    state = _current
    assert state is not None and state["task_id"] == task_id

    state["stage"] = "starting"
    state["started_at"] = time.time()

    # 落 Batch 行 — 随机模式下 DB.domain 存 'random' 占位,具体每号域名在 Account.email 里
    db_domain = "random" if random_pool else (domain or "")
    with SessionLocal() as db:
        b = Batch(id=state["batch_id"], domain=db_domain, count=count, status="running",
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
            # _persist_account 内部会从 info["domain"]/email 反推实际域名
            _persist_account(info, domain=domain)

    error_msg: str | None = None
    try:
        result = run_batch(
            count=count,
            domain=domain,
            random_pool=random_pool,
            progress_cb=_cb,
            batch_id=state["batch_id"],
        )
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

        # 根据 domain_mode 选择域名策略
        single_domain: str | None = None
        random_pool: list[str] | None = None
        display_domain: str

        if params.domain_mode == "fixed":
            if not params.domain:
                raise HTTPException(400, "domain_mode=fixed 时必须提供 domain")
            single_domain = params.domain
            display_domain = params.domain
        elif params.domain_mode == "random":
            random_pool = _enabled_domains(db)
            display_domain = "random"
        else:  # rotate (default)
            single_domain = params.domain or _pick_domain(db)
            display_domain = single_domain

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

        # 清掉历史 stop 信号
        from autofree.core.control import reset_stop
        reset_stop()

        t = threading.Thread(
            target=_runner,
            args=(task_id, params.count, single_domain, random_pool),
            name=f"freegen-{task_id}", daemon=True,
        )
        _current["thread"] = t
        t.start()

    return {
        "task_id": task_id, "batch_id": batch_id,
        "domain": display_domain, "domain_mode": params.domain_mode,
        "random_pool": random_pool or [],
        "count": params.count,
    }


class ResumeParams(BaseModel):
    email: str = Field(..., min_length=1)


@router.post("/resume", status_code=202)
def resume(
    params: ResumeParams,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    email = params.email.strip()
    """对一个 pending 账号「继续验证」: 用已有的 email/password 重跑 OAuth + phone gate。

    成功 → INSERT Account + push CPA + mark pending resolved。
    失败 → 留在 pending,只更新 error。
    """
    from autofree.db.models import PendingAccount

    pending_row = db.execute(
        select(PendingAccount).where(
            PendingAccount.email == email,
            PendingAccount.resolved_at.is_(None),
        )
    ).scalars().first()
    if not pending_row:
        raise HTTPException(404, "未找到该 pending 账号")
    # password 留空 → 走 email-only OTP 登录(从 cloud-mail 取 OTP)

    global _current
    with _lock:
        if _current and _current.get("stage") not in ("finished", "stopped", "failed"):
            raise HTTPException(409, "已有任务在运行,请等待结束或先 stop")

        task_id = uuid.uuid4().hex[:12]
        _current = {
            "task_id": task_id,
            "batch_id": pending_row.batch_id,  # 复用原 batch_id,新 Account 仍归属原批次
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
            "mode": "resume",
        }

        from autofree.core.control import reset_stop
        reset_stop()

        t = threading.Thread(
            target=_runner_resume,
            args=(task_id, email, pending_row.password or None),
            name=f"resume-{task_id}", daemon=True,
        )
        _current["thread"] = t
        t.start()

    return {"task_id": task_id, "email": email, "batch_id": pending_row.batch_id, "mode": "resume"}


class ManualAddItem(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    # 默认走「email 邮件 OTP」登录,不需要密码;前端表单只填 email
    password: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _strip_email(cls, v):
        v = (v or "").strip().lower()
        if "@" not in v:
            raise ValueError("email 格式不对")
        return v


class ManualAddParams(BaseModel):
    accounts: list[ManualAddItem] = Field(..., min_length=1, max_length=100)


@router.post("/manual-add", status_code=202)
def manual_add(
    params: ManualAddParams,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    """手动添加已有的 email+password — AutoFree 走 magic-link 登录拿 codex token,自动推 CPA。

    成功 → INSERT Account(同时已推 CPA);失败 → INSERT PendingAccount(可在「待办」页继续验证)。
    会建一个 domain='manual' 的 Batch 行,以便在「注册批次」页看进度。
    """
    # 去重 + 过滤掉已经在 Account 表里的(防止重复跑)
    seen: set[str] = set()
    items: list[tuple[str, str | None]] = []
    skipped_existing: list[str] = []
    skipped_duplicate: list[str] = []

    existing_emails = {
        e for (e,) in db.execute(select(Account.email)).all()
    }

    for a in params.accounts:
        if a.email in seen:
            skipped_duplicate.append(a.email)
            continue
        seen.add(a.email)
        if a.email in existing_emails:
            skipped_existing.append(a.email)
            continue
        # password=None → 走 email-only OTP 登录(走 cloud-mail 取 OTP)
        items.append((a.email, a.password or None))

    if not items:
        raise HTTPException(400,
            f"没有可处理的号 — "
            f"已存在于本地: {len(skipped_existing)},去重: {len(skipped_duplicate)}",
        )

    global _current
    with _lock:
        if _current and _current.get("stage") not in ("finished", "stopped", "failed"):
            raise HTTPException(409, "已有任务在运行,请等待结束或先 stop")

        task_id = uuid.uuid4().hex[:12]
        batch_id = uuid.uuid4().hex[:12]
        _current = {
            "task_id": task_id,
            "batch_id": batch_id,
            "stage": "pending",
            "index": 0,
            "total": len(items),
            "ok": 0,
            "failed": 0,
            "current_email": "",
            "events": [],
            "stop_requested": False,
            "thread": None,
            "started_at": None,
            "mode": "manual_add",
        }

        from autofree.core.control import reset_stop
        reset_stop()

        t = threading.Thread(
            target=_runner_manual_batch,
            args=(task_id, items),
            name=f"manual-add-{task_id}", daemon=True,
        )
        _current["thread"] = t
        t.start()

    return {
        "task_id": task_id,
        "batch_id": batch_id,
        "total": len(items),
        "skipped_existing": skipped_existing,
        "skipped_duplicate": skipped_duplicate,
        "mode": "manual_add",
    }


@router.post("/resume-all", status_code=202)
def resume_all(
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    """串行 resume 所有未解决的 pending(必须有密码才能跑)。

    返回 {task_id, total, skipped_no_password}。前端订阅 SSE 看进度。
    """
    pending_rows = db.execute(
        select(PendingAccount).where(PendingAccount.resolved_at.is_(None))
    ).scalars().all()

    # 全部纳入 — 缺密码的会自动走 email-only OTP 登录
    eligible: list[tuple[str, str | None, str]] = [
        (p.email, p.password or None, p.batch_id or "") for p in pending_rows
    ]
    skipped_no_password = 0  # 不再跳过

    if not eligible:
        raise HTTPException(400, "没有可继续验证的 pending")

    global _current
    with _lock:
        if _current and _current.get("stage") not in ("finished", "stopped", "failed"):
            raise HTTPException(409, "已有任务在运行,请等待结束或先 stop")

        task_id = uuid.uuid4().hex[:12]
        # 用第一个号的 batch_id 做占位(不会写新 Batch 行,纯粹串行 resume)
        _current = {
            "task_id": task_id,
            "batch_id": eligible[0][2],
            "stage": "pending",
            "index": 0,
            "total": len(eligible),
            "ok": 0,
            "failed": 0,
            "current_email": "",
            "events": [],
            "stop_requested": False,
            "thread": None,
            "started_at": None,
            "mode": "resume_all",
        }

        from autofree.core.control import reset_stop
        reset_stop()

        t = threading.Thread(
            target=_runner_resume_all,
            args=(task_id, eligible),
            name=f"resume-all-{task_id}", daemon=True,
        )
        _current["thread"] = t
        t.start()

    return {
        "task_id": task_id,
        "total": len(eligible),
        "skipped_no_password": skipped_no_password,
        "mode": "resume_all",
    }


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


@router.get("/batches/{batch_id}")
def batch_detail(
    batch_id: str,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    """批次详情:返回 batch 行 + 该批的 accounts + pending + results.json 全文。

    results.json 里有 register 阶段失败(没进 pending)的明细,前端需要 100% 可见。
    """
    from autofree.api.accounts import _serialize_account, _serialize_pending
    from autofree.db.models import Account, PendingAccount

    b = db.get(Batch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")

    accounts = db.execute(select(Account).where(Account.batch_id == batch_id)).scalars().all()
    pending = db.execute(
        select(PendingAccount).where(PendingAccount.batch_id == batch_id)
    ).scalars().all()

    # 找硬盘上的 results.json — batch_dir 命名 = batch_<TS>,这里按 started_at 反推
    raw_results: list[dict] = []
    settings = get_settings()
    if b.started_at:
        ts = b.started_at.strftime("%Y%m%d_%H%M%S")
        rp = settings.output_dir / f"batch_{ts}" / "results.json"
        if rp.exists():
            try:
                import json as _j
                raw_results = _j.loads(rp.read_text(encoding="utf-8")).get("results", [])
            except Exception:
                logger.exception("[freegen] 读 results.json 失败 path=%s", rp)

    cpa_pushed = sum(1 for a in accounts if a.cpa_synced)
    return {
        "batch": _serialize_batch(b),
        "accounts": [_serialize_account(a) for a in accounts],
        "pending": [_serialize_pending(p) for p in pending],
        "results": raw_results,
        "summary": {
            "total": b.count,
            "ok": b.ok,
            "failed": b.failed,
            "cpa_pushed": cpa_pushed,
            "cpa_unpushed": len(accounts) - cpa_pushed,
            "pending": len(pending),
        },
    }


@router.delete("/batches/{batch_id}", status_code=204)
def delete_batch(
    batch_id: str,
    drop_dir: bool = True,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> None:
    """删除批次 — 级联清 batch / accounts / pending,可选连硬盘 batch_<TS> 目录一起删。

    运行中的批次不允许删,防止误操作。
    """
    from autofree.db.models import Account, PendingAccount

    b = db.get(Batch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    if b.status == "running":
        raise HTTPException(409, "批次正在运行,请先停止再删除")

    db.execute(Account.__table__.delete().where(Account.batch_id == batch_id))
    db.execute(PendingAccount.__table__.delete().where(PendingAccount.batch_id == batch_id))

    started_ts = b.started_at.strftime("%Y%m%d_%H%M%S") if b.started_at else None
    db.delete(b)
    db.commit()

    if drop_dir and started_ts:
        import shutil
        settings = get_settings()
        d = settings.output_dir / f"batch_{started_ts}"
        if d.exists():
            try:
                shutil.rmtree(d)
                logger.info("[freegen] 删批次目录 %s", d)
            except Exception:
                logger.exception("[freegen] 删批次目录失败 path=%s", d)
