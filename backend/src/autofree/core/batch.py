"""freegen 批量 runner — 串行跑 N 个号,产出按本次 batch 时间戳归档。

每次启动新建 `freegen_output/batch_<ts>/`:
  - accounts.txt              所有成功账号(同名追加格式 email|password|account_id|plan_type|ts)
  - auth/<email>.json          每号 1 个 CPA-importable bundle
  - results.json               本次 batch 元数据 + 每号结果(ok/fail + 错误原因)

进度通过传入 `progress_cb(stage, info)` 暴露给上层(API 端写入 task["progress"])。
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autofree.core.config import OUTPUT_DIR, SCREENSHOT_DIR, assert_configured
from autofree.core.control import is_stop_requested, reset_stop
from autofree.core.errors import AccountDeactivated, BatchStopped, OAuthFailed, RegisterBlocked, RegisterFailed
from autofree.core.identity import random_password
from autofree.core.mail import MailClient
from autofree.core.oauth import fetch_personal_bundle
from autofree.core.register import register_account
from autofree.core.register_phone import PHONE_REG_PASSWORD, register_phone_and_fetch_bundle
from autofree.core.storage import append_account_line, append_pending_account, write_auth_json

logger = logging.getLogger(__name__)


ProgressCb = Callable[[str, dict[str, Any]], None]


def _new_batch_dir(batch_id: str | None = None) -> tuple[str, Path]:
    """生成 batch_id + 目录。

    batch_id 走外部传入(API 一般传 UUID,与 DB Batch 行同 ID,避免 FK 不匹配)。
    传入空时回退到 YYMMDD_HHMMSS 时间戳风格(老行为)。
    目录名永远基于时间戳,避免 UUID 在文件系统里没语义。
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    bid = (batch_id or "").strip() or ts
    out = OUTPUT_DIR / f"batch_{ts}"
    (out / "auth").mkdir(parents=True, exist_ok=True)
    return bid, out


def _write_results(batch_dir: Path, results: list[dict]) -> Path:
    path = batch_dir / "results.json"
    path.write_text(
        json.dumps({"results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def run_batch(
    *,
    count: int,
    domain: str | None = None,
    random_pool: list[str] | None = None,
    progress_cb: ProgressCb | None = None,
    batch_id: str | None = None,
    reg_mode: str = "email",
) -> dict:
    """串行跑 count 个号。

    域名选择:
    - random_pool 非空 → 每个号从池里随机抽一个域名(同批可混用多个域名)
    - 否则用单域名 domain(整批共用)

    reg_mode:
    - 'email'(默认)→ 现有路径,cloud-mail 邮箱注册 → fetch_personal_bundle 取 token
    - 'phone' → 新路径,SMS provider 买号 → 手机号注册 → 同一 SMS 订单 2 条 SMS 完成
      OAuth → /add-email 用 cloud-mail 邮箱 → 拿 token。账号 phone_verified=True

    返回 {batch_id, batch_dir, total, ok, failed, results: [...] }。
    单个号失败不影响后续。
    """
    reg_mode = (reg_mode or "email").strip().lower()
    if reg_mode not in ("email", "phone"):
        raise ValueError(f"reg_mode 必须是 email/phone,收到 {reg_mode!r}")
    if count <= 0:
        raise ValueError("count 必须 ≥ 1")

    # 归一化 random_pool
    pool: list[str] = []
    if random_pool:
        pool = [d.strip().lstrip("@") for d in random_pool if d and d.strip()]
        pool = [d for d in pool if d]

    if pool:
        def pick_domain() -> str:
            return random.choice(pool)
        display_domain = "random"
    else:
        single = (domain or "").strip().lstrip("@")
        if not single:
            raise ValueError("domain 不能为空(也未提供 random_pool)")
        def pick_domain() -> str:
            return single
        display_domain = single

    assert_configured()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from autofree.core.browser import cleanup_old_screenshots
        cleanup_old_screenshots(days=7)
    except Exception:
        logger.debug("[batch] cleanup_old_screenshots 失败(忽略)", exc_info=True)

    # 入口清掉上一轮残留的 stop 信号 — 否则上次按了 Stop 之后没人 reset,新 batch 一启动就被中断
    reset_stop()

    batch_id, batch_dir = _new_batch_dir(batch_id)
    logger.info("=" * 60)
    logger.info("[batch] 启动 batch_id=%s count=%d domain=%s reg_mode=%s out=%s",
                batch_id, count, display_domain, reg_mode, batch_dir)
    logger.info("=" * 60)

    def _emit(stage: str, info: dict[str, Any]) -> None:
        if progress_cb:
            try:
                progress_cb(stage, {"batch_id": batch_id, **info})
            except Exception:
                logger.debug("[batch] progress_cb 异常(忽略)", exc_info=True)

    _emit("started", {"count": count, "domain": display_domain, "batch_dir": str(batch_dir),
                      "reg_mode": reg_mode})

    mail = MailClient()
    mail.login()

    results: list[dict] = []
    ok_count = 0
    failed_count = 0

    stopped_early = False
    for i in range(1, count + 1):
        # 账号之间先看 stop 信号(用户在前一个号 OAuth 阶段按 Stop)
        if is_stop_requested():
            logger.warning("[batch] (%d/%d) 收到 stop — 不再启动后续账号", i, count)
            stopped_early = True
            _emit("stopped", {"index": i, "total": count, "reason": "stop_before_start"})
            break

        idx_info = {"index": i, "total": count}
        logger.info("[batch] (%d/%d) 开始", i, count)
        _emit("account_started", idx_info)

        address_id = None
        email = ""
        password = ""
        register_done = False  # 注册阶段是否完成 — 决定失败时是否保留邮箱+写 pending
        record: dict[str, Any] = {"index": i, "ok": False}

        def _to_pending(error_kind: str, error: str, *, phone_paid: bool = False) -> None:
            """注册成功但 OAuth 失败 → 保留邮箱 + 写 pending,等用户手动认证。

            phone_paid=True 时,此号已通过手机验证(5sim 真实扣过费),resume 时优先且不要丢。
            """
            try:
                append_pending_account(
                    email=email, password=password, batch_id=batch_id,
                    error_kind=error_kind, error=error,
                    phone_verified=phone_paid,
                )
                if phone_paid:
                    logger.info("[batch] %s 已加入 pending(💰 已付费/手机已验证)", email)
                else:
                    logger.info("[batch] %s 已加入 pending(邮箱保留,可手动 OAuth)", email)
            except Exception:
                logger.exception("[batch] append_pending_account 失败 — 至少 results.json 还有")

        def _drop_email() -> None:
            """注册阶段就失败 → 邮箱没用,删掉省 CloudMail 空间。"""
            if address_id:
                try: mail.delete_email(address_id)
                except Exception: pass

        try:
            current_domain = pick_domain()
            address_id, email = mail.create_email(domain=current_domain)
            # 手机号路径用固定密码(便于用户手动登录救号),邮箱路径仍用随机密码
            password = PHONE_REG_PASSWORD if reg_mode == "phone" else random_password()
            record.update({"email": email, "password": password, "domain": current_domain,
                           "reg_mode": reg_mode})
            logger.info("[batch] (%d/%d) 邮箱=%s 域名=%s reg_mode=%s",
                        i, count, email, current_domain, reg_mode)

            if reg_mode == "phone":
                # 手机号路径:register + OAuth + add-email + token 一个调用搞定
                # 共享 1 个 SMS 订单 / 2 条 SMS。返回 bundle 已带 phone_verified=True 和 phone。
                t0 = time.time()
                try:
                    bundle = register_phone_and_fetch_bundle(
                        email=email, password=password, mail_client=mail,
                    )
                except (RegisterFailed, RegisterBlocked):
                    # Phase 1 失败:OpenAI 上还没账号,email 没被绑过 → 安全 drop
                    raise
                except OAuthFailed:
                    # Phase 2 失败:Phase 1 已完成(OpenAI 上账号已建),写 pending
                    register_done = True
                    raise
                register_secs = time.time() - t0
                oauth_secs = 0.0  # phone 路径 OAuth 时间合并在 register_secs
                register_done = True
            else:
                # 邮箱路径(原有行为,1 字未改)
                t0 = time.time()
                ok, session_token = register_account(mail, email, password)
                register_secs = time.time() - t0
                if not ok:
                    raise RegisterFailed("register_account 返回 False")
                register_done = True

                t1 = time.time()
                bundle = fetch_personal_bundle(
                    email=email, password=password, mail_client=mail, session_token=session_token,
                )
                oauth_secs = time.time() - t1

            # 1) 写 auth.json — token 权威备份
            auth_path = write_auth_json(bundle, output_dir=batch_dir)

            # 2) CPA push 包 try/except — push 失败也不丢号
            cpa_ok = False
            cpa_msg = ""
            try:
                from autofree.core.cpa_push import push_auth_file
                cpa_ok, cpa_msg = push_auth_file(auth_path)
                logger.info("[batch] (%d/%d) CPA push: %s", i, count, cpa_msg)
            except Exception as exc:
                cpa_msg = f"CPA push 异常: {exc}"
                logger.exception("[batch] (%d/%d) CPA push 抛异常,继续插 DB", i, count)

            record.update({
                "ok": True,
                "account_id": bundle.get("account_id") or "",
                "plan_type": bundle.get("plan_type") or "free",
                "auth_file": auth_path.name,
                "register_secs": round(register_secs, 1),
                "oauth_secs": round(oauth_secs, 1),
                "cpa_pushed": cpa_ok,
                "cpa_msg": cpa_msg,
            })
            ok_count += 1
            logger.info("[batch] (%d/%d) ✅ %s plan=%s acct=%s",
                        i, count, email, bundle.get("plan_type"), bundle.get("account_id"))
            # 3) emit → DB 持久化(_persist_account 在 progress_cb 里跑)
            _emit("account_done", {
                **idx_info, "ok": True, "email": email,
                "password": password or "", "batch_id": batch_id,
                "account_id": bundle.get("account_id") or "",
                "plan_type": bundle.get("plan_type") or "free",
                "access_token": bundle.get("access_token") or "",
                "refresh_token": bundle.get("refresh_token") or "",
                "id_token": bundle.get("id_token") or "",
                "expires_at": bundle.get("expires_at"),
                "auth_file": auth_path.name,
                "auth_json_path": str(auth_path),
                "cpa_pushed": cpa_ok,
                "cpa_msg": cpa_msg,
                # 是否本次走 5sim 完成手机验证 — DB 写 Account.phone_verified
                "phone_verified": bool(bundle.get("phone_verified")),
            })

            # 4) accounts.txt 追加 — 失败也不丢号(token 已落 auth.json + DB + CPA)
            try:
                append_account_line(
                    email=email,
                    password=password,
                    account_id=bundle.get("account_id") or "",
                    plan_type=bundle.get("plan_type") or "free",
                    output_dir=batch_dir,
                )
            except Exception as exc:
                logger.warning("[batch] (%d/%d) accounts.txt 追加失败(忽略): %s", i, count, exc)

        except RegisterBlocked as exc:
            kind = "phone" if exc.is_phone else "duplicate" if exc.is_duplicate else "blocked"
            phone_paid = bool(getattr(exc, "phone_paid_via_sms", False))
            record["error"] = f"blocked: {exc}"
            record["error_kind"] = kind
            record["phone_verified"] = phone_paid
            failed_count += 1
            logger.error("[batch] (%d/%d) ❌ blocked: %s%s", i, count, exc,
                         " (💰 phone 已付费)" if phone_paid else "")
            _emit("account_done", {**idx_info, "ok": False, "email": email,
                                   "password": password, "batch_id": batch_id,
                                   "error": str(exc), "error_kind": kind,
                                   "phone_verified": phone_paid,
                                   "register_done": register_done})
            if register_done:
                _to_pending(kind, str(exc), phone_paid=phone_paid)
            else:
                _drop_email()
        except RegisterFailed as exc:
            record["error"] = f"register_failed: {exc}"
            record["error_kind"] = "register"
            failed_count += 1
            logger.error("[batch] (%d/%d) ❌ register_failed: %s", i, count, exc)
            _emit("account_done", {**idx_info, "ok": False, "email": email,
                                   "password": password, "batch_id": batch_id,
                                   "error": str(exc), "error_kind": "register",
                                   "register_done": register_done})
            _drop_email()
        except AccountDeactivated as exc:
            # 终结性:号已被 OpenAI 停用,reauth 无意义。不写 pending,标记 error_kind=deactivated。
            phone_paid = bool(getattr(exc, "phone_paid_via_sms", False))
            record["error"] = f"account_deactivated: {exc}"
            record["error_kind"] = "deactivated"
            record["phone_verified"] = phone_paid
            failed_count += 1
            logger.error("[batch] (%d/%d) 🪦 deactivated: %s%s", i, count, exc,
                         " (💰 phone 已付费但号已废)" if phone_paid else "")
            _emit("account_done", {**idx_info, "ok": False, "email": email,
                                   "password": password, "batch_id": batch_id,
                                   "error": str(exc), "error_kind": "deactivated",
                                   "phone_verified": phone_paid,
                                   "register_done": register_done})
        except OAuthFailed as exc:
            phone_paid = bool(getattr(exc, "phone_paid_via_sms", False))
            record["error"] = f"oauth_failed: {exc}"
            record["error_kind"] = "oauth"
            record["phone_verified"] = phone_paid
            failed_count += 1
            tag = "💰 oauth_failed (phone 已付费)" if phone_paid else "❌ oauth_failed"
            logger.error("[batch] (%d/%d) %s: %s", i, count, tag, exc)
            _emit("account_done", {**idx_info, "ok": False, "email": email,
                                   "password": password, "batch_id": batch_id,
                                   "error": str(exc), "error_kind": "oauth",
                                   "phone_verified": phone_paid,
                                   "register_done": register_done})
            if register_done:
                _to_pending("oauth", str(exc), phone_paid=phone_paid)
            # OAuthFailed 必然 register_done=True (fetch_personal_bundle 才会抛),无 else
        except BatchStopped as exc:
            # 用户中断 — 当前账号记 stopped;若已注册成功也写 pending(可后续手动 OAuth)
            phone_paid = bool(getattr(exc, "phone_paid_via_sms", False))
            record["error"] = f"stopped: {exc}"
            record["error_kind"] = "stopped"
            record["phone_verified"] = phone_paid
            failed_count += 1
            logger.warning("[batch] (%d/%d) ⏹ stopped: %s%s", i, count, exc,
                           " (💰 phone 已付费)" if phone_paid else "")
            _emit("account_done", {**idx_info, "ok": False, "email": email,
                                   "password": password, "batch_id": batch_id,
                                   "error": str(exc), "error_kind": "stopped",
                                   "phone_verified": phone_paid,
                                   "register_done": register_done})
            if register_done:
                _to_pending("stopped", str(exc), phone_paid=phone_paid)
            else:
                _drop_email()
            results.append(record)
            _write_results(batch_dir, results)
            stopped_early = True
            _emit("stopped", {"index": i, "total": count, "reason": "stop_during_account"})
            break
        except Exception as exc:
            phone_paid = bool(getattr(exc, "phone_paid_via_sms", False))
            record["error"] = f"unexpected: {exc}"
            record["error_kind"] = "unexpected"
            record["phone_verified"] = phone_paid
            failed_count += 1
            logger.exception("[batch] (%d/%d) ❌ unexpected", i, count)
            _emit("account_done", {**idx_info, "ok": False, "email": email,
                                   "password": password, "batch_id": batch_id,
                                   "error": str(exc), "error_kind": "unexpected",
                                   "phone_verified": phone_paid,
                                   "register_done": register_done})
            if register_done:
                _to_pending("unexpected", str(exc), phone_paid=phone_paid)
            else:
                _drop_email()

        results.append(record)
        # 每个账号写一次 results.json,中途崩了也保留进度
        _write_results(batch_dir, results)

    summary = {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "domain": display_domain,
        "total": count,
        "ok": ok_count,
        "failed": failed_count,
        "results": results,
        "finished_at": time.time(),
        "stopped": stopped_early,
    }
    # 最终再写一次,带 summary 元数据
    (batch_dir / "results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("=" * 60)
    logger.info("[batch] %s ok=%d failed=%d (out=%s)",
                "已中断" if stopped_early else "完成", ok_count, failed_count, batch_dir)
    logger.info("=" * 60)
    _emit("finished", {"ok": ok_count, "failed": failed_count,
                       "batch_dir": str(batch_dir), "stopped": stopped_early})
    return summary


def run_single_resume(
    *,
    email: str,
    password: str | None,
    batch_id: str,
    progress_cb: ProgressCb | None = None,
) -> dict:
    """单号「继续验证 / 手动添加」:对一个已存在的 OpenAI 号跑 OAuth + phone gate。

    password 可选:
    - 有密码 → 走 email + 密码 + 可能的 OTP 路径
    - password=None → 走纯 email + cloud-mail OTP 路径(手动添加场景)

    不重新创建邮箱、不重新注册。直接打开浏览器走 fetch_personal_bundle:
    - session_token=None
    - 登录后若进 phone gate → 用 5sim 拿号
    - 成功 → 写 auth/<email>.json + accounts.txt(追加)
    - 失败 → 抛 RegisterBlocked / OAuthFailed

    返回 {ok, email, batch_id, auth_path?, error?, error_kind?}
    """
    if not email:
        raise ValueError("email 不能为空")

    assert_configured()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    reset_stop()

    # 复用一个 batch 目录(基于当前时间戳),不污染原 batch 的 auth 目录
    _, batch_dir = _new_batch_dir(batch_id)

    def _emit(stage: str, info: dict[str, Any]) -> None:
        if progress_cb:
            try:
                progress_cb(stage, {"batch_id": batch_id, **info})
            except Exception:
                logger.debug("[resume] progress_cb 异常(忽略)", exc_info=True)

    _emit("started", {"count": 1, "domain": email.split("@", 1)[-1] if "@" in email else "",
                      "batch_dir": str(batch_dir), "mode": "resume"})

    mail = MailClient()
    mail.login()

    record: dict[str, Any] = {"index": 1, "ok": False, "email": email, "password": password or ""}
    idx_info = {"index": 1, "total": 1}
    _emit("account_started", {**idx_info, "email": email, "mode": "resume"})

    try:
        t1 = time.time()
        bundle = fetch_personal_bundle(
            email=email, password=password, mail_client=mail, session_token=None,
        )
        oauth_secs = time.time() - t1

        # 1) 写 auth.json — 这是 token 的"权威备份",最优先,失败直接抛
        auth_path = write_auth_json(bundle, output_dir=batch_dir)

        # 2) CPA push — 包 try/except,push 失败也不丢号(record 仍 ok=True,cpa_error 标失败)
        cpa_ok = False
        cpa_msg = ""
        try:
            from autofree.core.cpa_push import push_auth_file
            cpa_ok, cpa_msg = push_auth_file(auth_path)
            logger.info("[resume] %s OAuth 成功 → CPA push: %s", email, cpa_msg)
        except Exception as exc:
            cpa_msg = f"CPA push 异常: {exc}"
            logger.exception("[resume] CPA push 抛异常,继续插 DB")

        # 3) emit account_done — 触发 _persist_account 把 Account 写 DB(关键!)
        record.update({
            "ok": True,
            "account_id": bundle.get("account_id") or "",
            "plan_type": bundle.get("plan_type") or "free",
            "auth_file": auth_path.name,
            "auth_path": str(auth_path),
            "oauth_secs": round(oauth_secs, 1),
            "cpa_pushed": cpa_ok,
            "cpa_msg": cpa_msg,
        })
        _emit("account_done", {
            **idx_info, "ok": True, "email": email, "password": password or "",
            "batch_id": batch_id,
            "account_id": bundle.get("account_id") or "",
            "plan_type": bundle.get("plan_type") or "free",
            "access_token": bundle.get("access_token") or "",
            "refresh_token": bundle.get("refresh_token") or "",
            "id_token": bundle.get("id_token") or "",
            "expires_at": bundle.get("expires_at"),
            "auth_file": auth_path.name,
            "auth_json_path": str(auth_path),
            "cpa_pushed": cpa_ok,
            "cpa_msg": cpa_msg,
            # resume 成功 = 此号已通过手机验证(无论是历史已验证还是这次 resume 又过了 phone gate)
            "phone_verified": True,
            "register_done": True,  # 注册早就完成了,这条只为保持 schema 一致
            "mode": "resume",
        })

        # 4) accounts.txt 追加 — 失败也不影响(token 已落 auth.json + DB + CPA)
        try:
            append_account_line(
                email=email, password=password,
                account_id=bundle.get("account_id") or "",
                plan_type=bundle.get("plan_type") or "free",
                output_dir=batch_dir,
            )
        except Exception as exc:
            logger.warning("[resume] %s accounts.txt 追加失败(忽略): %s", email, exc)

        _emit("finished", {"ok": 1, "failed": 0, "batch_dir": str(batch_dir),
                           "stopped": False, "mode": "resume"})
        return record

    except RegisterBlocked as exc:
        phone_paid = bool(getattr(exc, "phone_paid_via_sms", False))
        kind = "phone" if exc.is_phone else "duplicate" if exc.is_duplicate else "blocked"
        record["error"] = f"blocked: {exc}"
        record["error_kind"] = kind
        record["phone_verified"] = phone_paid
        logger.error("[resume] %s ❌ blocked: %s%s", email, exc,
                     " (💰 phone 已付费)" if phone_paid else "")
        _emit("account_done", {**idx_info, "ok": False, "email": email, "password": password,
                               "batch_id": batch_id, "error": str(exc), "error_kind": kind,
                               "phone_verified": phone_paid,
                               "register_done": True, "mode": "resume"})
        _emit("finished", {"ok": 0, "failed": 1, "batch_dir": str(batch_dir),
                           "stopped": False, "mode": "resume"})
        return record
    except AccountDeactivated as exc:
        # 终结:号已废,不再写 pending(reauth 无意义)
        phone_paid = bool(getattr(exc, "phone_paid_via_sms", False))
        record["error"] = f"account_deactivated: {exc}"
        record["error_kind"] = "deactivated"
        record["phone_verified"] = phone_paid
        logger.error("[resume] %s 🪦 deactivated: %s", email, exc)
        _emit("account_done", {**idx_info, "ok": False, "email": email, "password": password,
                               "batch_id": batch_id, "error": str(exc), "error_kind": "deactivated",
                               "phone_verified": phone_paid,
                               "register_done": True, "mode": "resume"})
        _emit("finished", {"ok": 0, "failed": 1, "batch_dir": str(batch_dir),
                           "stopped": False, "mode": "resume"})
        return record
    except OAuthFailed as exc:
        phone_paid = bool(getattr(exc, "phone_paid_via_sms", False))
        record["error"] = f"oauth_failed: {exc}"
        record["error_kind"] = "oauth"
        record["phone_verified"] = phone_paid
        logger.error("[resume] %s ❌ oauth_failed: %s%s", email, exc,
                     " (💰 phone 已付费)" if phone_paid else "")
        _emit("account_done", {**idx_info, "ok": False, "email": email, "password": password,
                               "batch_id": batch_id, "error": str(exc), "error_kind": "oauth",
                               "phone_verified": phone_paid,
                               "register_done": True, "mode": "resume"})
        _emit("finished", {"ok": 0, "failed": 1, "batch_dir": str(batch_dir),
                           "stopped": False, "mode": "resume"})
        return record
    except BatchStopped as exc:
        record["error"] = f"stopped: {exc}"
        record["error_kind"] = "stopped"
        logger.warning("[resume] %s ⏹ stopped: %s", email, exc)
        _emit("account_done", {**idx_info, "ok": False, "email": email, "password": password,
                               "batch_id": batch_id, "error": str(exc), "error_kind": "stopped",
                               "register_done": True, "mode": "resume"})
        _emit("stopped", {"index": 1, "total": 1, "reason": "stop_during_account"})
        _emit("finished", {"ok": 0, "failed": 1, "batch_dir": str(batch_dir),
                           "stopped": True, "mode": "resume"})
        return record
    except Exception as exc:
        record["error"] = f"unexpected: {exc}"
        record["error_kind"] = "unexpected"
        logger.exception("[resume] %s ❌ unexpected", email)
        _emit("account_done", {**idx_info, "ok": False, "email": email, "password": password,
                               "batch_id": batch_id, "error": str(exc), "error_kind": "unexpected",
                               "register_done": True, "mode": "resume"})
        _emit("finished", {"ok": 0, "failed": 1, "batch_dir": str(batch_dir),
                           "stopped": False, "mode": "resume"})
        return record


def list_batches() -> list[dict]:
    """扫 OUTPUT_DIR 下所有 batch_* 目录,返回元数据列表(按时间倒序)。

    每项:{batch_id, batch_dir, count, ok, failed, finished_at, has_summary}
    """
    if not OUTPUT_DIR.exists():
        return []
    items = []
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not d.is_dir() or not d.name.startswith("batch_"):
            continue
        results_path = d / "results.json"
        item = {
            "batch_id": d.name.removeprefix("batch_"),
            "batch_dir": str(d),
            "count": 0,
            "ok": 0,
            "failed": 0,
            "finished_at": None,
            "has_summary": False,
        }
        if results_path.exists():
            try:
                data = json.loads(results_path.read_text(encoding="utf-8"))
                if "results" in data and isinstance(data["results"], list):
                    item["count"] = data.get("total", len(data["results"]))
                    item["ok"] = data.get("ok", sum(1 for r in data["results"] if r.get("ok")))
                    item["failed"] = data.get("failed", sum(1 for r in data["results"] if not r.get("ok")))
                    item["finished_at"] = data.get("finished_at")
                    item["has_summary"] = "finished_at" in data
            except Exception:
                logger.warning("[batch] 解析 %s 失败", results_path)
        items.append(item)
    return items


def load_batch(batch_id: str) -> dict:
    """读单个 batch 的 results.json(含 results 数组)。找不到 raise FileNotFoundError。"""
    safe = batch_id.replace("/", "_").replace("..", "")
    d = OUTPUT_DIR / f"batch_{safe}"
    p = d / "results.json"
    if not p.exists():
        raise FileNotFoundError(f"batch {batch_id} 不存在 ({p})")
    return json.loads(p.read_text(encoding="utf-8"))


def get_batch_auth_file(batch_id: str, email: str) -> Path:
    """返回某 batch 内某邮箱的 auth.json 绝对路径。找不到 raise FileNotFoundError。"""
    safe = batch_id.replace("/", "_").replace("..", "")
    safe_email = email.replace("/", "_")
    p = OUTPUT_DIR / f"batch_{safe}" / "auth" / f"{safe_email}.json"
    if not p.exists():
        raise FileNotFoundError(f"auth file 不存在 ({p})")
    return p
