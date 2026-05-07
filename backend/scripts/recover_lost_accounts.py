"""一次性恢复脚本 — 把 cloud-mail 上「曾收过 OpenAI 邮件」但 DB 里没记录的号批量
写入 pending_account,以便用户在「待办」页一键 resume(走 email-only OAuth)。

背景:
- schema 缺 phone_verified 列时,_persist_account 写库报错被吞 → 大量已付费号丢失
- 但 OAuthFailed 路径不调 _drop_email,邮箱仍在 cloud-mail
- 这些号 OpenAI 端账号已建,phone 已绑(若走过 phone gate),只缺 token
- 走 email-only OAuth(password=None)即可登录 → 拿 token → 推 CPA

用法(在生产服务器上):
    # 1) 进容器
    docker compose exec app bash
    # 2) 干跑(不动 DB,只打印)
    python /app/scripts/recover_lost_accounts.py --dry-run
    # 3) 确认无误后真跑
    python /app/scripts/recover_lost_accounts.py --apply
    # 可选:限制最多导入多少个(测试用)
    python /app/scripts/recover_lost_accounts.py --apply --limit 5

跑完之后,UI「待办」页应该多出一批 error_kind=oauth_recover、💰 已付费 标识的号,
点「一键继续全部」即可。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
import time
from pathlib import Path

# 让脚本在容器内 / 直接 python 跑都能 import autofree
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sqlalchemy import select  # noqa: E402

from autofree.core.config import get_mail_config  # noqa: E402
from autofree.core.mail import MailClient  # noqa: E402
from autofree.db.base import SessionLocal  # noqa: E402
from autofree.db.models import Account, Batch, PendingAccount  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("recover")


# 邮件 raw 里出现这些关键字之一 → 判定为 OpenAI 发的注册邮件
OPENAI_MARKERS = (
    "tm.openai.com",
    "ptr.openai.com",
    "openai.com",
    "verification code",
    "Welcome to OpenAI",
    "ChatGPT",
)


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


def is_openai_mail(raw: str) -> bool:
    if not raw:
        return False
    low = raw.lower()
    return any(k.lower() in low for k in OPENAI_MARKERS)


def fetch_all_addresses(mc: MailClient) -> list[dict]:
    """拉 cloud-mail 上所有地址(分页)。"""
    out: list[dict] = []
    for off in range(0, 50000, 100):
        r = mc.session.get(
            mc._url("/admin/address"),
            headers=mc._headers(),
            params={"limit": 100, "offset": off},
            timeout=30,
        )
        if r.status_code != 200:
            logger.error("拉地址列表失败 HTTP %s: %s", r.status_code, r.text[:200])
            break
        rows = (r.json() or {}).get("results") or []
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 100:
            break
    return out


def addr_has_openai_mail(mc: MailClient, address: str) -> tuple[bool, int]:
    """看该邮箱是否收到过 OpenAI 邮件。返回 (是否有, 邮件总数)。"""
    r = mc.session.get(
        mc._url("/admin/mails"),
        headers=mc._headers(),
        params={"limit": 10, "offset": 0, "address": address},
        timeout=30,
    )
    if r.status_code != 200:
        return False, 0
    rows = (r.json() or {}).get("results") or []
    if not rows:
        return False, 0
    has = any(is_openai_mail(m.get("raw") or "") for m in rows)
    return has, len(rows)


def ensure_recover_batch(db, batch_id: str) -> Batch:
    """创建/获取一个特殊 batch 行,所有恢复号挂在下面。"""
    existing = db.execute(select(Batch).where(Batch.id == batch_id)).scalar_one_or_none()
    if existing:
        return existing
    b = Batch(
        id=batch_id,
        domain="(recover)",
        count=0,
        status="finished",
        started_at=_utcnow(),
        finished_at=_utcnow(),
        ok=0,
        failed=0,
    )
    db.add(b)
    db.flush()
    return b


def main():
    ap = argparse.ArgumentParser(description="cloud-mail → pending_account 恢复脚本")
    ap.add_argument("--apply", action="store_true",
                    help="真写库(默认 dry-run 只打印)")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多导入多少个(0=不限制,用于测试)")
    ap.add_argument("--skip-mail-check", action="store_true",
                    help="跳过 OpenAI 邮件验证 — 只要 mail_count>0 就当注册成功 "
                         "(快但宽松;默认 strict 会逐封 raw 抓 openai.com)")
    args = ap.parse_args()

    cfg = get_mail_config()
    if not cfg["base_url"] or not cfg["password"]:
        logger.error("cloud-mail 未配置 — 请先在 UI 设置页填好 base_url + admin_password")
        sys.exit(1)
    logger.info("cloud-mail base=%s", cfg["base_url"])

    mc = MailClient()
    mc.login()

    # 1) 拉全部地址
    addrs = fetch_all_addresses(mc)
    logger.info("cloud-mail 现存地址总数: %d", len(addrs))
    if not addrs:
        logger.warning("cloud-mail 无地址,退出")
        return

    # 2) 取 DB 已知 email(成功 + 已 pending),作为排除集
    with SessionLocal() as db:
        known_account = {row[0] for row in db.execute(select(Account.email)).all()}
        known_pending = {row[0] for row in db.execute(select(PendingAccount.email)).all()}
    known_all = known_account | known_pending
    logger.info("DB 已知 account=%d pending=%d 合计 %d",
                len(known_account), len(known_pending), len(known_all))

    # 3) 候选号 = cloud-mail 全集 - DB 已知
    candidates = []
    for a in addrs:
        addr = (a.get("name") or a.get("address") or "").strip().lower()
        if "@" not in addr:
            continue
        if addr in known_all:
            continue
        candidates.append({
            "email": addr,
            "id": a.get("id"),
            "mail_count": int(a.get("mail_count") or 0),
            "created_at": a.get("created_at") or "",
        })
    logger.info("初筛候选(cloud-mail 有 / DB 没): %d", len(candidates))

    # 4) 用 OpenAI 邮件做严格筛选(可关)
    if args.skip_mail_check:
        confirmed = [c for c in candidates if c["mail_count"] > 0]
        logger.info("[skip-mail-check] 仅按 mail_count>0 筛: %d", len(confirmed))
    else:
        logger.info("逐个查 OpenAI 邮件(strict)...")
        confirmed = []
        for i, c in enumerate(candidates):
            if c["mail_count"] == 0:
                continue
            ok, n = addr_has_openai_mail(mc, c["email"])
            if ok:
                confirmed.append(c)
            if (i + 1) % 25 == 0:
                logger.info("  进度 %d/%d 已确认 %d", i + 1, len(candidates), len(confirmed))
            time.sleep(0.05)
        logger.info("OpenAI 邮件验证后: %d", len(confirmed))

    if args.limit > 0:
        confirmed = confirmed[:args.limit]
        logger.info("--limit %d → 截到 %d", args.limit, len(confirmed))

    # 5) 输出报告 + (apply 时)写库
    print()
    print("=" * 60)
    print(f"将恢复的号: {len(confirmed)}")
    print("=" * 60)
    if not confirmed:
        print("无可恢复号,退出")
        return

    print(f"前 10 条样本:")
    for c in confirmed[:10]:
        print(f"  {c['email']}  mail_count={c['mail_count']}  cloud_mail_id={c['id']}")
    print()

    if not args.apply:
        print("[DRY-RUN] 没有写库。加 --apply 真跑。")
        return

    batch_id = "recover_" + time.strftime("%y%m%d%H%M")
    inserted = 0
    skipped = 0
    err_msg = "schema bug 导致丢失,从 cloud-mail 恢复 — 走 email-only OAuth"
    with SessionLocal() as db:
        ensure_recover_batch(db, batch_id)
        for c in confirmed:
            existed = db.execute(
                select(PendingAccount).where(PendingAccount.email == c["email"])
            ).scalar_one_or_none()
            if existed:
                skipped += 1
                continue
            db.add(PendingAccount(
                batch_id=batch_id,
                email=c["email"],
                password="",  # email-only resume 不需要密码
                error_kind="oauth_recover",
                error=err_msg,
                phone_verified=True,  # 假定都付费过(实际 resume 时若需要 phone gate 会自动救援)
                phone_verified_at=_utcnow(),
            ))
            inserted += 1
        db.commit()

    print(f"[APPLY] 已写入 pending_account: {inserted} 条,跳过 {skipped} 条(已存在)")
    print(f"batch_id = {batch_id}")
    print()
    print("下一步:")
    print("  1. 打开 UI「待办」页,会看到这批 💰 已付费 / oauth_recover 号")
    print("  2. 点「一键继续全部」让程序串行 resume(走 email-only OAuth)")
    print("  3. 看着日志 / 截图目录;若某号撞 phone gate 重新出现,会自动用 5sim 救援")


if __name__ == "__main__":
    main()
