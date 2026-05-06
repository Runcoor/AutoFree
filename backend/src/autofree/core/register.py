"""ChatGPT.com 直接注册(纯 personal,不依赖任何 Team workspace)。

提供 `register_account(mail_client, email, password)` → True/False(成功才能进 OAuth)。
内部用 Playwright 跑全流程:邮箱 → 密码 → OTP → about-you → 成功。
"""

from __future__ import annotations

import logging
import time

from playwright.sync_api import sync_playwright

from autofree.core.browser import (
    assert_not_blocked,
    click_primary_button,
    first_visible_editable,
    get_context_options,
    get_launch_options,
    get_proxy_options,
    is_google_redirect,
    make_proxy_session_id,
    page_excerpt,
    safe_screenshot,
    wait_cloudflare,
)
from autofree.core.config import EMAIL_POLL_TIMEOUT, SCREENSHOT_DIR
from autofree.core.errors import RegisterBlocked, RegisterFailed
from autofree.core.identity import random_birthday, random_full_name

logger = logging.getLogger(__name__)


_EMAIL_SELECTORS = (
    'input[name="email"], input[type="email"], input[id="email"], '
    'input[autocomplete="email"], input[autocomplete="username"], '
    'input[placeholder*="email" i], input[placeholder*="Email" i]'
)
_PASSWORD_SELECTORS = 'input[name="password"], input[type="password"]'
_CODE_SELECTORS = 'input[name="code"], input[placeholder*="验证码"], input[placeholder*="code" i]'

SIGNUP_URL = "https://chatgpt.com/auth/login"


def _detect_step(page) -> str:
    """返回当前阶段:email / password / code / profile / completed / google / unknown。"""
    url = (page.url or "").lower()
    if is_google_redirect(page):
        return "google"
    if "email-verification" in url:
        return "code"
    if "about-you" in url:
        return "profile"
    if "create-account/password" in url or url.endswith("/password"):
        return "password"
    if "chatgpt.com" in url and "auth" not in url:
        return "completed"

    if first_visible_editable(page, _PASSWORD_SELECTORS, timeout=300):
        return "password"
    if first_visible_editable(page, _CODE_SELECTORS, timeout=300):
        return "code"
    try:
        if page.locator('input[name="name"], [role="spinbutton"]').first.is_visible(timeout=300):
            return "profile"
    except Exception:
        pass
    if first_visible_editable(page, _EMAIL_SELECTORS, timeout=300):
        return "email"
    if "log-in-or-create-account" in url or url.endswith("/auth/login"):
        return "email"
    if "create-account" in url or "password" in url:
        return "password"
    return "unknown"


def _wait_step_in(page, allowed: set[str], timeout: int = 15) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _detect_step(page)
        if s in allowed:
            return s
        time.sleep(0.5)
    return _detect_step(page)


def _wait_step_change(page, current: str, timeout: int = 15) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _detect_step(page)
        if s != current:
            return s
        time.sleep(0.5)
    return _detect_step(page)


def _open_signup_form(page) -> None:
    """初始化页可能是登录页 / 多种 A/B 变体;尝试点 Sign up 等按钮把邮箱框露出来。"""
    try:
        if page.locator(_EMAIL_SELECTORS).first.is_visible(timeout=3000):
            return
    except Exception:
        pass
    for sel, desc in (
        ('button:has-text("More options")', "More options"),
        ('button:has-text("更多选项")', "更多选项"),
        ('a:has-text("Sign up for free")', "Sign up for free"),
        ('button:has-text("Sign up for free")', "Sign up for free"),
        ('a:has-text("Sign up")', "Sign up"),
        ('button:has-text("Sign up")', "Sign up"),
        ('a:has-text("注册")', "注册"),
        ('button:has-text("注册")', "注册"),
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                logger.info("[register] 点击: %s", desc)
                btn.click()
                time.sleep(2)
                if _wait_step_in(page, {"email", "password", "code", "profile", "completed", "google"}, timeout=10) != "unknown":
                    return
        except Exception:
            continue


def _fill_email(page, email: str) -> None:
    for attempt in range(3):
        if _detect_step(page) != "email":
            return
        ei = first_visible_editable(page, _EMAIL_SELECTORS, timeout=1500)
        if not ei:
            if _wait_step_change(page, "email", timeout=10) != "email":
                return
            continue
        ei.fill(email)
        time.sleep(0.5)
        click_primary_button(page, ei, ["Continue", "继续"])
        next_step = _wait_step_change(page, "email", timeout=15)
        logger.info("[register] 提交邮箱后状态: %s | URL: %s", next_step, page.url)
        if next_step == "google":
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            continue
        if next_step != "email":
            return
    raise RegisterFailed(f"邮箱步骤未推进 | URL={page.url} | body={page_excerpt(page)}")


def _fill_password(page, password: str) -> None:
    for attempt in range(2):
        if _detect_step(page) != "password":
            return
        pi = first_visible_editable(page, _PASSWORD_SELECTORS, timeout=1500)
        if not pi:
            if _wait_step_change(page, "password", timeout=10) != "password":
                return
            continue
        pi.fill(password)
        time.sleep(0.5)
        click_primary_button(page, pi, ["Continue", "继续", "Log in"])
        next_step = _wait_step_change(page, "password", timeout=15)
        logger.info("[register] 提交密码后状态: %s | URL: %s", next_step, page.url)
        if next_step == "google":
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            continue
        if next_step != "password":
            return
    raise RegisterFailed(f"密码步骤未推进 | URL={page.url}")


def _fill_otp(page, mail_client, email: str, *, mail_baseline_id: int) -> None:
    """等 OTP 邮件并填入。"""
    try:
        ci = page.locator(_CODE_SELECTORS).first
        if not ci.is_visible(timeout=5000):
            return  # 没要 OTP 直接过
    except Exception:
        return

    logger.info("[register] 等待 OTP 邮件 (after_id=%d, timeout=%ds)", mail_baseline_id, EMAIL_POLL_TIMEOUT)
    _, code = mail_client.wait_for_otp(
        email,
        after_id=mail_baseline_id,
        timeout=EMAIL_POLL_TIMEOUT,
    )
    logger.info("[register] 输入 OTP: %s", code)
    ci.fill(code)
    time.sleep(0.5)
    click_primary_button(page, ci, ["Continue", "继续"])
    time.sleep(8)


_NAME_SELECTORS = (
    'input[name="name"]',
    'input[name="full_name"]',
    'input[autocomplete="name"]',
    'input[placeholder="Full name"]',
    'input[placeholder*="Full name" i]',
    'input[placeholder*="name" i]',
    'input[placeholder*="姓名"]',
)
_AGE_SELECTORS = (
    'input[name="age"]',
    'input[placeholder="Age"]',
    'input[placeholder*="Age" i]',
    'input[placeholder*="age" i]',
    'input[placeholder*="年龄"]',
)
# 注意:故意不加 'input[type="number"]' — 新版生日 spinbutton 也是 type=number,
# 会误中并把 age_str 塞到月份字段(导致 "Hmm, that doesn't look right")
_SUBMIT_SELECTORS = (
    'button:has-text("Finish creating account")',
    'button:has-text("完成帐户创建")',
    'button:has-text("完成创建帐户")',
    'button:has-text("Create account")',
    'button:has-text("Continue")',
    'button:has-text("继续")',
    'button[type="submit"]',
)


class AboutYouAlreadyExists(Exception):
    """OpenAI 后端在 about-you 阶段抛 user_already_exists — 账号其实已创建,可直接进 OAuth。"""


def _try_fill(page, selectors: tuple, value: str, label: str) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500) and el.is_editable(timeout=500):
                el.fill(value)
                logger.info("[register] 填 %s: %s (sel=%s)", label, value, sel)
                time.sleep(0.3)
                return True
        except Exception:
            continue
    return False


def _classify_birthday_field(sb) -> str | None:
    """根据 aria-label / placeholder 判断 spinbutton 是 month / day / year 哪一个。"""
    try:
        label = (sb.get_attribute("aria-label") or "").lower()
    except Exception:
        label = ""
    try:
        placeholder = (sb.get_attribute("placeholder") or "").lower()
    except Exception:
        placeholder = ""
    try:
        name = (sb.get_attribute("name") or "").lower()
    except Exception:
        name = ""
    blob = f"{label} {placeholder} {name}"
    # year 优先判断(yyyy / yy / year),避免 "day" 也包含 "y"
    if "year" in blob or "yyyy" in placeholder or "yy" == placeholder:
        return "year"
    if "month" in blob or placeholder == "mm" or "mm/" in placeholder:
        return "month"
    if "day" in blob or placeholder == "dd" or "dd/" in placeholder:
        return "day"
    return None


def _fill_one_spinbutton(page, sb, value: str) -> None:
    """点中 spinbutton → 全选清空 → 输入 value。带多重 fallback。"""
    try:
        sb.click(force=True)
        time.sleep(0.15)
    except Exception:
        pass
    # 三种清空策略,谁好用谁来
    try:
        # 优先用 fill (Playwright 自带:先 select-all 再 type)
        sb.fill(value)
        return
    except Exception:
        pass
    # fallback: Ctrl/Meta+A 全选 → type
    cleared = False
    for keys in ("Control+a", "Meta+a", "ControlOrMeta+a"):
        try:
            page.keyboard.press(keys)
            time.sleep(0.05)
            cleared = True
            break
        except Exception:
            continue
    if not cleared:
        # 再 fallback: triple click 选中 + Backspace
        try:
            sb.click(click_count=3, force=True)
            page.keyboard.press("Backspace")
        except Exception:
            pass
    page.keyboard.type(value, delay=60)
    time.sleep(0.2)


def _fill_birthday_spinbuttons(page, bday: dict) -> None:
    """填新版 about-you 的 3 个生日 spinbutton — 按 aria-label / placeholder 识别字段位置,
    不假设 MM/DD/YYYY 或其他顺序。识别不到的话回退到原始位置 (month, day, year)。
    """
    try:
        spinbuttons = page.locator('[role="spinbutton"]').all()
    except Exception:
        spinbuttons = []

    if len(spinbuttons) < 3:
        # 退一步:有些变体直接用 input[type=number] 不带 role,按 placeholder 找
        try:
            spinbuttons = []
            for tag in ("month", "day", "year"):
                el = page.locator(
                    f'input[placeholder*="{tag}" i], input[aria-label*="{tag}" i]'
                ).first
                if el.is_visible(timeout=300):
                    spinbuttons.append(el)
        except Exception:
            spinbuttons = []

    if not spinbuttons:
        logger.warning("[register] 没找到 age input 也没找到生日字段 — 表单可能改版")
        return

    slots: dict[str, object] = {}
    for sb in spinbuttons:
        kind = _classify_birthday_field(sb)
        if kind and kind not in slots:
            slots[kind] = sb

    try:
        if len(slots) >= 3:
            # 识别到了:按字段名填
            _fill_one_spinbutton(page, slots["month"], bday["month"])
            _fill_one_spinbutton(page, slots["day"], bday["day"])
            _fill_one_spinbutton(page, slots["year"], bday["year"])
            logger.info(
                "[register] 填生日 (按字段): MM=%s DD=%s YYYY=%s",
                bday["month"], bday["day"], bday["year"],
            )
            return
    except Exception as exc:
        logger.warning("[register] 按字段填生日失败,回退位置法: %s", exc)

    # 回退:按位置(美式 MM / DD / YYYY 顺序)
    if len(spinbuttons) >= 3:
        positional = (bday["month"], bday["day"], bday["year"])
        try:
            for sb, val in zip(spinbuttons[:3], positional):
                _fill_one_spinbutton(page, sb, val)
            logger.info(
                "[register] 填生日 (位置法 MM/DD/YYYY): %s/%s/%s",
                bday["month"], bday["day"], bday["year"],
            )
        except Exception as exc:
            logger.warning("[register] 位置法填生日也失败: %s", exc)
    else:
        logger.warning(
            "[register] spinbutton 不足 3 个 (找到 %d),无法填生日", len(spinbuttons),
        )


def _detect_about_you_error(page) -> str | None:
    """返回终结性错误关键字(user_already_exists 等),没有错误返回 None。"""
    try:
        body = page.inner_text("body")[:800].lower()
    except Exception:
        return None
    if "user_already_exists" in body:
        return "user_already_exists"
    if "an error occurred during authentication" in body:
        return "auth_error"
    return None


def _complete_about_you(page) -> None:
    """about-you 页面 — 单次提交策略:多 selector 找字段 → 一次性填好 → 提交一次 → 等长一些。

    PoC 经验:多次 retry 提交会触发 user_already_exists(后端在第 1 次 click 时已建账号,
    后续 click 重复创建)。改为"只点一次,失败就抛 AboutYouAlreadyExists 让上游恢复"。
    """
    if "about-you" not in (page.url or "").lower():
        return

    name = random_full_name()

    # 1) 填姓名
    if not _try_fill(page, _NAME_SELECTORS, name, "姓名"):
        # fallback: 第一个 visible text input
        try:
            for inp in page.locator("input").all():
                try:
                    t = (inp.get_attribute("type") or "text").lower()
                    if t in ("text", "") and inp.is_editable(timeout=300) and inp.is_visible(timeout=300):
                        inp.fill(name)
                        logger.info("[register] 填姓名 (fallback first input): %s", name)
                        break
                except Exception:
                    continue
            else:
                logger.warning("[register] 姓名字段未找到,空着提交可能被 reject")
        except Exception:
            pass

    # 2) 填年龄(老 A/B:单 age 字段) 或 生日 spinbutton(新 A/B:MM/DD/YYYY)
    bday = random_birthday()
    age_str = str(int(time.strftime("%Y")) - int(bday["year"]))  # 由 birthday year 反推 age
    age_filled = _try_fill(page, _AGE_SELECTORS, age_str, "年龄")
    if not age_filled:
        _fill_birthday_spinbuttons(page, bday)

    # 提交前截一张图,失败时方便看到我们填的具体值
    try:
        safe_screenshot(page, SCREENSHOT_DIR / "06a_about_you_filled.png")
    except Exception:
        pass

    # 3) 提交一次(只一次!)
    submitted = False
    for sel in _SUBMIT_SELECTORS:
        try:
            b = page.locator(sel).first
            if b.is_visible(timeout=1500):
                b.click()
                submitted = True
                logger.info("[register] 提交 about-you (sel=%s)", sel)
                break
        except Exception:
            continue
    if not submitted:
        try:
            page.keyboard.press("Enter")
            logger.info("[register] 兜底 Enter 提交 about-you")
        except Exception:
            logger.warning("[register] 完全没找到 about-you 提交按钮")

    # 4) 等结果 — 30s 长等待。终结状态:completed (注册成 chatgpt.com 主页) / error 出现 / phone block
    deadline = time.time() + 30
    while time.time() < deadline:
        # terminal error?
        err = _detect_about_you_error(page)
        if err:
            logger.warning("[register] about-you 提交后撞 %s — 账号可能已建,移交 OAuth 处理", err)
            raise AboutYouAlreadyExists(err)
        # terminal success?
        url_low = (page.url or "").lower()
        if "about-you" not in url_low:
            logger.info("[register] about-you 已离开,新 URL: %s", page.url)
            assert_not_blocked(page, "about_you_navigated")
            return
        time.sleep(1)

    logger.warning("[register] about-you 30s 无变化,URL 仍 %s | body=%s", page.url, page_excerpt(page))


def _extract_session_token(context) -> str | None:
    """从 chatgpt.com cookies 抽 __Secure-next-auth.session-token,可能切片为 .0/.1。

    Round 11 经验:大 token (>3.8KB) 被 NextAuth 自动切两段,需按 suffix 数字排序拼回。
    """
    try:
        cookies = context.cookies()
    except Exception as exc:
        logger.warning("[register] 抽 session_token cookies 异常: %s", exc)
        return None

    session_token = None
    parts: dict[str, str] = {}
    for c in cookies:
        name = c.get("name", "")
        if name == "__Secure-next-auth.session-token":
            session_token = c.get("value", "")
        elif name.startswith("__Secure-next-auth.session-token."):
            suffix = name.rsplit(".", 1)[-1]
            parts[suffix] = c.get("value", "")

    if not session_token and parts:
        session_token = "".join(parts[k] for k in sorted(parts))

    if session_token:
        logger.info("[register] session_token 已抽出 len=%d (用于 OAuth 跳过 /log-in)", len(session_token))
    else:
        logger.warning("[register] 未发现 __Secure-next-auth.session-token cookie — OAuth 可能撞 add-phone")
    return session_token or None


def register_account(mail_client, email: str, password: str) -> tuple[bool, str | None]:
    """注册 1 个 chatgpt.com personal 账号。

    返回 (success, session_token)。session_token 用于 OAuth 阶段注入,跳过 /log-in 避开 add-phone。
    PoC 单次尝试,不内置重试。失败时 caller 直接放弃该号(创建新邮箱重来)。
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    mail_baseline_id = mail_client.latest_mail_id(email)
    logger.info("[register] 开始 %s (mail_baseline_id=%d)", email, mail_baseline_id)

    proxy_session_id = make_proxy_session_id(prefix=email.split("@", 1)[0])
    proxy_opts = get_proxy_options(session_id=proxy_session_id)
    launch_kwargs = get_launch_options()
    if proxy_opts:
        launch_kwargs["proxy"] = proxy_opts
        logger.info("[register] 使用代理 session=%s server=%s", proxy_session_id, proxy_opts["server"])

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(**get_context_options())
        page = context.new_page()

        try:
            # Cloudflare 重试:单次失败 close page → new page → 重 goto。
            # 同 IP 多次 goto 有概率拿到软挑战(turnstile 算法随机)。
            cf_ok = False
            for cf_attempt in range(3):
                if cf_attempt > 0:
                    logger.info("[register] cf 第 %d 次重试,关页面重开...", cf_attempt + 1)
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = context.new_page()
                page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)
                cf_ok = wait_cloudflare(page, max_wait_seconds=90)
                safe_screenshot(page, SCREENSHOT_DIR / f"01_login_page_attempt{cf_attempt + 1}.png")
                if cf_ok:
                    break
                logger.warning("[register] cf 第 %d 次未过", cf_attempt + 1)

            if not cf_ok:
                raise RegisterFailed(
                    f"Cloudflare turnstile 3 次重试仍未通过 — 极可能是 IP 信誉问题(数据中心 IP)。"
                    f"必须挂住宅代理(IPRoyal 等)或换带住宅 IP 的机器。截图: "
                    f"{SCREENSHOT_DIR / '01_login_page_attempt*.png'}"
                )

            _open_signup_form(page)
            safe_screenshot(page, SCREENSHOT_DIR / "02_signup.png")

            # 点完 Sign up for free 通常跳到 auth.openai.com,它自己有 Cloudflare full-page
            # 拦截("Performing security verification")。再等一次 CF。
            cf_ok2 = wait_cloudflare(page, max_wait_seconds=60)
            safe_screenshot(page, SCREENSHOT_DIR / "02b_after_cf2.png")
            if not cf_ok2:
                raise RegisterFailed(
                    f"auth.openai.com Cloudflare 60s 内未通过 — IP 信誉问题。"
                    f"截图: {SCREENSHOT_DIR / '02b_after_cf2.png'}"
                )

            init_step = _wait_step_in(page, {"email", "password", "code", "profile", "completed", "google"}, timeout=15)
            logger.info("[register] 邮箱步骤初始状态: %s | URL: %s", init_step, page.url)
            if init_step == "google":
                raise RegisterFailed(f"误跳转 Google: {page.url}")
            if init_step == "unknown":
                raise RegisterFailed(f"未识别初始步骤: {page.url} | body={page_excerpt(page)}")

            _fill_email(page, email)
            assert_not_blocked(page, "email_submit")
            safe_screenshot(page, SCREENSHOT_DIR / "03_after_email.png")

            _wait_step_in(page, {"password", "code", "profile", "completed", "google", "email"}, timeout=15)
            _fill_password(page, password)
            assert_not_blocked(page, "password_submit")
            safe_screenshot(page, SCREENSHOT_DIR / "04_after_password.png")

            _fill_otp(page, mail_client, email, mail_baseline_id=mail_baseline_id)
            safe_screenshot(page, SCREENSHOT_DIR / "05_after_code.png")
            assert_not_blocked(page, "code_submit")

            account_already_created = False
            try:
                _complete_about_you(page)
            except RegisterBlocked:
                raise
            except AboutYouAlreadyExists as exc:
                # 账号已经在后端创建,只是 about-you 没收到 ack。
                # 这种情况 OAuth 用同一个 email + password 应该能成功登录拿 bundle。
                logger.info("[register] 账号已建(about-you ack 丢失):%s — 让 OAuth 接管", exc)
                account_already_created = True
            except Exception as exc:
                logger.warning("[register] about-you 异常: %s | URL=%s", exc, page.url)
            safe_screenshot(page, SCREENSHOT_DIR / "06_after_profile.png")

            # 末端可能有 Accept/Join workspace 按钮(若域名挂在某 Team)— 我们不点,因为
            # PoC 走纯 personal,如果触发 join 说明域名其实不是 fresh,需要换域名。
            current_url = page.url
            landed_chatgpt = "chatgpt.com" in current_url and "auth" not in current_url and not is_google_redirect(page)
            success = landed_chatgpt or account_already_created
            if success:
                if landed_chatgpt:
                    logger.info("[register] 注册成功(已落 chatgpt.com): %s", current_url)
                else:
                    logger.info("[register] 注册视为成功(账号已建,OAuth 接管): %s", current_url)
            else:
                logger.warning("[register] 注册可能未完成: %s | body=%s", current_url, page_excerpt(page))
            safe_screenshot(page, SCREENSHOT_DIR / "07_final.png")

            # 在 close browser 之前抽 session_token,后续 OAuth 注入跳过 /log-in
            session_token = _extract_session_token(context) if success else None
            return success, session_token
        finally:
            try:
                browser.close()
            except Exception:
                pass
