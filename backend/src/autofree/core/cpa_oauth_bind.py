"""CPA 对接 — 补绑邮箱 OAuth 自动化。

场景:
- 已注册 phone-only 号(AutoFree DB 有 phone_e164 + password + email)
- CPA(或其他系统)生成 authorize URL 给 AutoFree
- AutoFree 用 Playwright 跑完整 OAuth:phone → password 登录 → /add-email
  绑 cloud-mail → callback
- 返回完整 callback URL(含 ?code=&state=)给调用方,调用方用自己的
  PKCE code_verifier 换 token(AutoFree 不持 verifier,只驱动浏览器)

关键差异 vs 注册时 _phase2_oauth:
1. 接收 *外部* authorize URL,不自己生成 PKCE
2. 全程 0 SMS — phone + password 登录路径(phase1 设的固定密码 v7zw8ai29r4ZA)
3. 不换 token,返回 callback URL 即可

返回:{ callback_url, email_bound, sms_used, phone_used }
"""

from __future__ import annotations

import logging
import time
import urllib.parse

from playwright.sync_api import sync_playwright

from autofree.core.browser import (
    email_screenshot_scope,
    get_context_options,
    get_launch_options,
    get_proxy_options,
    make_proxy_session_id,
    safe_screenshot,
    wait_cloudflare,
)
from autofree.core.config import EMAIL_POLL_TIMEOUT, SCREENSHOT_DIR
from autofree.core.control import is_stop_requested
from autofree.core.errors import BatchStopped, OAuthFailed
from autofree.core.identity import random_birthday, random_full_name
from autofree.core.oauth import assert_account_alive
from autofree.core.phone_country import PhoneCountry, from_e164, strip_dial_prefix
from autofree.core.register_phone import (
    ALLOW_BUTTON_TEXTS,
    PASSWORD_INPUT_SELECTOR,
    PHONE_LOGIN_TEXTS,
    _classify_password_page,
    _click_button_by_text,
    _click_submit_button,
    _detect_oauth_error,
    _fill_about_you,
    _fill_password_input,
    _fill_phone_input,
    _fill_sms_code_smart,
    _select_country,
    _sleep,
)

logger = logging.getLogger(__name__)


def _extract_full_callback(url: str) -> str | None:
    """从 URL 提取完整 callback(含 ?code=&state=)。返 None 表示不是 callback。"""
    if not url:
        return None
    url_low = url.lower()
    if "/auth/callback" not in url_low:
        return None
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if qs.get("code", [None])[0]:
        return url
    return None


def bind_email_via_external_oauth(
    *,
    auth_url: str,
    phone_e164: str,
    password: str,
    email_for_bind: str,
    mail_client,
    country_iso: str = "",
) -> dict:
    """跑外部 authorize URL 走完整 OAuth 绑邮箱,返回 callback URL。

    参数:
      auth_url       — 外部系统(CPA)给的 https://auth.openai.com/oauth/authorize?...
                      必须含 prompt=login,否则会走 picker shortcut 跳过 /add-email
      phone_e164     — Account.phone_e164(注册时的手机号)
      password       — Account.password(注册时设的 v7zw8ai29r4ZA)
      email_for_bind — Account.email(cloud-mail 邮箱,用于 /add-email)
      mail_client    — cloud-mail 客户端,收 /email-verification OTP
      country_iso    — 可选,空则从 phone_e164 反推

    返回 dict:
      callback_url  — 完整 callback URL,含 code + state(调用方换 token 用)
      email_bound   — /add-email 是否成功绑了邮箱
      phone_used    — 实际登录用的手机号
      sms_used      — 是否走了 SMS 兜底(本流程预期 False)
    """
    if not auth_url or "auth.openai.com" not in auth_url.lower():
        raise OAuthFailed(f"auth_url 非法或不是 auth.openai.com: {auth_url[:80]}")
    if "prompt=login" not in auth_url.lower():
        logger.warning(
            "[cpa-bind] ⚠️ auth_url 不含 prompt=login,可能走 picker shortcut 跳过 /add-email — 继续但可能失败"
        )
    if not phone_e164:
        raise OAuthFailed("account.phone_e164 为空 — 此号不是 phone-reg 注册,无法用 phone 登录补绑")
    if not password:
        raise OAuthFailed("account.password 为空 — 没设密码,补绑必须 SMS(暂不支持,请走手动)")
    if not email_for_bind:
        raise OAuthFailed("email_for_bind 为空")

    country = _resolve_country(country_iso, phone_e164)
    logger.info(
        "[cpa-bind] 开始补绑 phone=%s country=%s/%s email=%s",
        phone_e164, country.iso_code, country.dial_code, email_for_bind,
    )

    captured_callback: list[str | None] = [None]

    proxy_session_id = make_proxy_session_id(prefix=email_for_bind.split("@", 1)[0])
    proxy_opts = get_proxy_options(session_id=proxy_session_id)
    launch_kwargs = get_launch_options()
    if proxy_opts:
        launch_kwargs["proxy"] = proxy_opts
        logger.info("[cpa-bind] 使用代理 session=%s", proxy_session_id)

    with email_screenshot_scope(email_for_bind) as _ss_dir, sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(**get_context_options())
        page = context.new_page()

        def _try_capture(u: str, source: str) -> None:
            if captured_callback[0]:
                return
            cb = _extract_full_callback(u)
            if cb:
                captured_callback[0] = cb
                logger.info("[cpa-bind] 捕获 callback URL (%s) url=%s", source, cb[:120])

        page.on("request", lambda req: _try_capture(req.url, "request"))
        page.on("requestfailed", lambda req: _try_capture(req.url, "requestfailed"))
        page.on("response", lambda res: _try_capture(res.url, "response"))
        page.on("framenavigated", lambda f: _try_capture(f.url, "framenav"))

        try:
            mail_baseline_id = mail_client.latest_mail_id(email_for_bind)
        except Exception:
            mail_baseline_id = 0
        logger.info("[cpa-bind] cloud-mail baseline id=%d", mail_baseline_id)

        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
            _sleep(3)
            wait_cloudflare(page, max_wait_seconds=90)
            safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_01_loaded.png")

            email_bound = False
            sms_used = False
            last_url = ""

            for round_idx in range(40):
                if is_stop_requested():
                    raise BatchStopped("cpa-bind 收到 stop")

                _sleep(3)

                # 截到 callback 就完成
                try:
                    _try_capture(page.url or "", "live_url")
                except Exception:
                    pass
                if captured_callback[0]:
                    if email_bound:
                        logger.info("[cpa-bind] ✅ callback 已捕获(email 已绑)")
                    else:
                        logger.warning(
                            "[cpa-bind] ⚠️ callback 已捕获但 /add-email 未触发 — auth_url "
                            "可能没带 prompt=login 走了 picker shortcut。code 可能无效。"
                        )
                    return {
                        "callback_url": captured_callback[0],
                        "email_bound": email_bound,
                        "phone_used": phone_e164,
                        "sms_used": sms_used,
                    }

                try:
                    url = page.url or ""
                    url_low = url.lower()
                    inputs_info = page.evaluate(
                        """() => Array.from(document.querySelectorAll('input:not([type=hidden])')).map(i => ({
                            type: i.type, name: i.name, placeholder: i.placeholder || '',
                        }))"""
                    )
                    btns_info = page.evaluate(
                        """() => Array.from(document.querySelectorAll('button')).map(b => (b.innerText || '').trim()).filter(t => t)"""
                    )
                    body_text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 600)")
                except Exception:
                    last_url = ""
                    continue

                if "chrome-error" in url_low:
                    # localhost:1455 拒连 — request 事件已截到 URL,等 capture
                    _sleep(2)
                    continue

                try:
                    assert_account_alive(page, f"cpa_bind_r{round_idx}")
                except Exception:
                    raise

                err_marker = _detect_oauth_error(page)
                if err_marker:
                    logger.warning("[cpa-bind] r%d OAuth 错误页 marker=%s", round_idx, err_marker)
                    raise OAuthFailed(f"OAuth 错误页: {err_marker}")

                if url == last_url:
                    continue
                input_types = [i.get("type") for i in inputs_info if i.get("type")]
                logger.info(
                    "[cpa-bind] [DIAG] r%d url=%s inputs=%s btns=%s body[:200]=%r",
                    round_idx, url[:120], input_types[:8], btns_info[:10], (body_text or "")[:200],
                )

                # 1) 登录方式选择页(可能有「使用手机登录」按钮)
                has_phone_login_btn = any(
                    any(t in b for t in PHONE_LOGIN_TEXTS) for b in btns_info
                )
                has_phone_input = any(
                    i.get("name") == "phoneNumberInput" or i.get("type") == "tel"
                    for i in inputs_info
                )
                if has_phone_login_btn and not has_phone_input:
                    logger.info("[cpa-bind] r%d 点「继续使用手机登录」", round_idx)
                    _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=8000)
                    _sleep(3)
                    last_url = url
                    continue

                # 2) 手机号输入页
                if has_phone_input:
                    logger.info("[cpa-bind] r%d 手机号输入页 — 填 %s", round_idx, phone_e164)
                    try:
                        _select_country(page, country)
                    except Exception:
                        pass
                    local_number = strip_dial_prefix(phone_e164, country)
                    try:
                        _fill_phone_input(page, local_number, phone_e164, country)
                    except Exception as exc:
                        raise OAuthFailed(f"手机号填写失败: {exc}") from exc
                    _click_submit_button(page)
                    _sleep(3)
                    wait_cloudflare(page, max_wait_seconds=30)
                    _sleep(2)
                    safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_02_phone_submit.png")
                    last_url = url
                    continue

                # 3) SMS 验证码页 — 我们走 password-only,撞到 SMS 直接失败
                is_code_page = (
                    "contact-verification" in url_low or "phone-verification" in url_low
                    or any(
                        (i.get("type") in ("text", "tel", "number") and i.get("name") != "phoneNumberInput"
                         and ("code" in (i.get("name") or "").lower()
                              or "code" in (i.get("placeholder") or "").lower()
                              or any(s in (i.get("placeholder") or "").lower()
                                     for s in ("verification", "验证"))))
                        for i in inputs_info
                    )
                )
                if is_code_page:
                    safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_03_sms_page.png")
                    raise OAuthFailed(
                        f"OAuth 要求 SMS 验证,但补绑流程走 password-only 路径,无 SMS provider。"
                        f"原因:OpenAI 风控判定此号需 2FA,或注册时密码未设成功。url={url[:80]}"
                    )

                # 4) /add-email
                if "add-email" in url_low or "add_email" in url_low:
                    logger.info("[cpa-bind] r%d /add-email,绑 %s", round_idx, email_for_bind)
                    try:
                        _fill_add_email(page, email_for_bind)
                    except Exception as exc:
                        safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_04_add_email_failed.png")
                        raise OAuthFailed(f"/add-email 填写失败: {exc}") from exc
                    _click_submit_button(page)
                    _sleep(4)
                    wait_cloudflare(page, max_wait_seconds=30)
                    _sleep(2)
                    email_bound = True
                    safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_05_add_email_submit.png")
                    last_url = url
                    continue

                # 5) /email-verification — cloud-mail OTP
                is_email_otp = (
                    "email-verification" in url_low
                    or (email_bound and any(
                        i.get("type") in ("text", "tel", "number") and i.get("name") != "phoneNumberInput"
                        for i in inputs_info
                    ) and ("code" in body_text.lower() or "verification" in body_text.lower()
                           or "验证码" in body_text))
                )
                if is_email_otp:
                    logger.info("[cpa-bind] r%d /email-verification,等 cloud-mail OTP", round_idx)
                    try:
                        _, mail_code = mail_client.wait_for_otp(
                            email_for_bind, after_id=mail_baseline_id, timeout=EMAIL_POLL_TIMEOUT,
                        )
                    except Exception as exc:
                        raise OAuthFailed(f"cloud-mail OTP 超时: {exc}") from exc
                    if not _fill_sms_code_smart(page, mail_code):
                        safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_06_otp_fill_failed.png")
                        raise OAuthFailed("/email-verification code 填写失败")
                    _click_submit_button(page)
                    _sleep(4)
                    wait_cloudflare(page, max_wait_seconds=30)
                    _sleep(2)
                    safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_07_email_verified.png")
                    last_url = url
                    continue

                # 6) about-you(偶尔出现)
                if "about-you" in url_low or "about_you" in url_low:
                    logger.info("[cpa-bind] r%d about-you", round_idx)
                    _fill_about_you(page, random_full_name(), random_birthday())
                    _sleep(5)
                    wait_cloudflare(page, max_wait_seconds=30)
                    _sleep(2)
                    last_url = url
                    continue

                # 7) 密码页(分类):existing → 填密码登录,create 几乎不会出现(我们不是注册)
                is_pw_page = "password" in url_low or any(
                    i.get("type") == "password" for i in inputs_info
                )
                if is_pw_page:
                    pw_kind = _classify_password_page(page)
                    logger.info("[cpa-bind] r%d 密码页 pw_kind=%s url=%s", round_idx, pw_kind, url[:80])
                    try:
                        _fill_password_input(page, password)
                    except Exception as exc:
                        safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_08_pw_fill_failed.png")
                        raise OAuthFailed(f"密码填写失败: {exc}") from exc
                    _sleep(0.5)
                    try:
                        page.keyboard.press("Enter")
                    except Exception:
                        pass
                    _sleep(1)
                    _click_submit_button(page)
                    _sleep(4)
                    wait_cloudflare(page, max_wait_seconds=60)
                    _sleep(2)
                    # 检测密码错
                    try:
                        still_pw = page.locator(PASSWORD_INPUT_SELECTOR).first.is_visible(timeout=2000)
                    except Exception:
                        still_pw = False
                    if still_pw and "password" in (page.url or "").lower():
                        try:
                            body_low = (page.locator("body").inner_text(timeout=1500) or "").lower()
                        except Exception:
                            body_low = ""
                        err_markers = ("incorrect", "wrong password", "密码错", "密码不", "invalid password")
                        if any(m in body_low for m in err_markers):
                            raise OAuthFailed(
                                f"密码错误 — Account.password={password!r} 与 OpenAI 不一致"
                            )
                    last_url = url
                    continue

                # 8) consent 页
                if "/consent" in url_low or "/authorize" in url_low:
                    logger.info("[cpa-bind] r%d consent 页", round_idx)
                    if _click_button_by_text(page, ALLOW_BUTTON_TEXTS, timeout_ms=8000):
                        _sleep(4)
                        last_url = url
                        continue

                last_url = url

            safe_screenshot(page, SCREENSHOT_DIR / "cpa_bind_99_timeout.png")
            raise OAuthFailed(f"40 轮超时,最后 url={page.url}")

        finally:
            try:
                browser.close()
            except Exception:
                pass


def _resolve_country(country_iso: str, phone_e164: str) -> PhoneCountry:
    """优先用传入的 ISO,空则从手机号反推。"""
    from autofree.core.phone_country import from_iso
    if country_iso:
        return from_iso(country_iso)
    return from_e164(phone_e164)


def _fill_add_email(page, email: str) -> None:
    """JS focus + keyboard.type,避开 React Aria 浮动 label 拦截。"""
    deadline = time.monotonic() + 5
    ei_sel = ('input[type="email"], input[name="email"], '
              'input[name="username"], input[name="identifier"]')
    ready = False
    while time.monotonic() < deadline:
        if page.evaluate(f"() => !!document.querySelector('{ei_sel}')"):
            ready = True
            break
        time.sleep(0.3)
    if not ready:
        raise OAuthFailed("/add-email 找不到 email 输入框")
    focused = page.evaluate(
        """(s) => {
            const el = document.querySelector(s);
            if (!el) return false;
            el.focus();
            return document.activeElement === el;
        }""",
        ei_sel,
    )
    if not focused:
        page.locator(ei_sel).first.click(force=True, timeout=3000)
    time.sleep(0.2)
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    except Exception:
        pass
    page.keyboard.type(email, delay=30)
