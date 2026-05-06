"""personal codex OAuth — PKCE + Playwright consent loop。

针对刚注册的纯 personal 账号(从未 join 过 Team workspace),consent 应该一步直达,
不需要 silent step-0 / NextAuth refresh / 双域 cookie 注入这些 Team→Personal
翻转的复杂逻辑。

- CLIENT_ID / AUTH_URL / TOKEN_URL / REDIRECT_URI 与 codex CLI 官方一致
- bundle 输出:{access_token, refresh_token, id_token, account_id, email, plan_type, expires_at}
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse

import requests
from playwright.sync_api import sync_playwright

from autofree.core.browser import (
    assert_not_blocked,
    click_primary_button,
    detect_phone_block,
    get_context_options,
    get_launch_options,
    get_proxy_options,
    is_google_redirect,
    make_proxy_session_id,
    safe_screenshot,
    type_otp_code,
)
from autofree.core.config import EMAIL_POLL_TIMEOUT, SCREENSHOT_DIR, get_sms_config
from autofree.core.control import is_stop_requested
from autofree.core.errors import AccountDeactivated, BatchStopped, OAuthFailed, RegisterBlocked
from autofree.core import sms as sms_mod

logger = logging.getLogger(__name__)


CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _build_auth_url(challenge: str, state: str) -> str:
    # 关键: prompt=login (NOT consent) — 强制 auth.openai.com 走 /log-in 流程,
    # 在 auth domain 自然 mint workspace 到 OAuth session, 根治 no_valid_organizations.
    # 参考: tmp/oauthService.js:182 (JS 参考实现, 已知能 work).
    #
    # 历史方案曾用 prompt=consent + 失败时 stage-2 fresh re-login 兜底;
    # 既然每次都是新号, 直接 prompt=login 一步到位, 省掉 stage-2 的 250 行复杂度.
    params = {
        "client_id": CODEX_CLIENT_ID,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "codex_cli_simplified_flow": "true",
        "id_token_add_organizations": "true",
        "prompt": "login",
        "redirect_uri": CODEX_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile offline_access",
        "state": state,
    }
    return f"{CODEX_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8", errors="replace"))
    except Exception:
        return {}


def _exchange_code(auth_code: str, code_verifier: str, fallback_email: str) -> dict:
    """code → bundle dict。"""
    resp = requests.post(
        CODEX_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CODEX_CLIENT_ID,
            "code": auth_code,
            "redirect_uri": CODEX_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise OAuthFailed(f"token 交换失败 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    id_token = data.get("id_token", "")
    claims = _decode_jwt_payload(id_token)
    auth_claims = claims.get("https://api.openai.com/auth", {})
    bundle = {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "id_token": id_token,
        "account_id": auth_claims.get("chatgpt_account_id", ""),
        "email": claims.get("email", fallback_email),
        "plan_type": auth_claims.get("chatgpt_plan_type", "unknown"),
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
    }
    logger.info(
        "[oauth] token 交换成功 email=%s plan_type=%s account_id=%s exp_in=%ds",
        bundle["email"], bundle["plan_type"], bundle["account_id"], int(data.get("expires_in", 0)),
    )
    return bundle


def refresh_access_token(refresh_token: str) -> dict | None:
    """用 refresh_token 静默换一对新的 access/id_token — 1 次 HTTP, 无浏览器。

    成功返 {access_token, refresh_token, id_token, expires_in}, 失败返 None。
    """
    if not refresh_token:
        return None
    try:
        resp = requests.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CODEX_CLIENT_ID,
                "refresh_token": refresh_token,
                "scope": "openid profile email",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except Exception as exc:
        logger.warning("[oauth] refresh 网络异常: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("[oauth] refresh 失败 HTTP %d: %s", resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    return {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", refresh_token),
        "id_token": data.get("id_token", ""),
        "expires_in": int(data.get("expires_in", 3600)),
    }


def _inject_session_cookies(context, session_token: str) -> None:
    """把 chatgpt.com __Secure-next-auth.session-token 注入 chatgpt.com + auth.openai.com 双域。

    Round 11 经验:
    - 单注 auth.openai.com 不够 — NextAuth 跨域 issuer 校验严,/oauth/authorize 不认 chatgpt 颁发的 token
    - >3800 字节的大 token 必须切两段 .0 / .1
    - 注完先 goto chatgpt.com 让服务端 validate session,后端会自动写齐配套 cookies
      (oai-did / __cflb / cf_clearance / _puid) → /oauth/authorize 直接进 consent
    """
    def _build(domain: str) -> list[dict]:
        if len(session_token) > 3800:
            return [
                {"name": "__Secure-next-auth.session-token.0", "value": session_token[:3800],
                 "domain": domain, "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"},
                {"name": "__Secure-next-auth.session-token.1", "value": session_token[3800:],
                 "domain": domain, "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"},
            ]
        return [
            {"name": "__Secure-next-auth.session-token", "value": session_token,
             "domain": domain, "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"},
        ]

    cookies = _build("chatgpt.com") + _build("auth.openai.com")
    context.add_cookies(cookies)
    logger.info("[oauth] session_token 注入完成 (len=%d) chatgpt.com + auth.openai.com", len(session_token))


def _silent_step0(context, *, debug_screenshot_path) -> None:
    """先 goto chatgpt.com 让服务端 validate session,触发 NextAuth 写齐配套 cookies。

    然后调一次 /api/auth/session?update 主动刷新 session(踢出 Team 后必须刷 user.workspace,
    新注册号也走相同路径保持一致)。
    """
    p = context.new_page()
    try:
        p.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        # cf 拦截可能再来一次,等下
        from autofree.core.browser import wait_cloudflare
        wait_cloudflare(p, max_wait_seconds=60)
        # 强制 NextAuth session refresh
        try:
            r = p.evaluate(
                """async () => {
                    const r = await fetch('/api/auth/session?update', { credentials: 'include', cache: 'no-store' });
                    const ct = r.headers.get('content-type') || '';
                    if (!ct.includes('application/json')) return { ok: r.ok, status: r.status, raw: 'non-json' };
                    const data = await r.json();
                    return { ok: r.ok, status: r.status, hasUser: !!data?.user };
                }"""
            )
            logger.info("[oauth] silent step-0 NextAuth refresh: %s", r)
        except Exception as exc:
            logger.warning("[oauth] silent step-0 NextAuth refresh 异常(忽略): %s", exc)
        safe_screenshot(p, debug_screenshot_path)
    except Exception as exc:
        logger.warning("[oauth] silent step-0 异常(继续走 OAuth): %s", exc)
    finally:
        try:
            p.close()
        except Exception:
            pass


# ─── 终结性账号错误检测 ─────────────────────────────────────────────────────
# OpenAI 在 auth 页面会以 banner / inline error 形式返回这些 code,撞到任一个都说明
# 号已废,reauth 无意义。提早抛 AccountDeactivated 跳过 phone gate / consent,省 5sim。
_TERMINAL_ACCOUNT_ERRORS = (
    "account_deactivated",
    "account_disabled",
    "account_blocked",
    "user_disabled",
)


def detect_account_deactivated(page) -> str | None:
    """扫页面 body / URL 找终结性 account_* 错误码。返中招的 code,没找到返 None。"""
    try:
        body = (page.inner_text("body", timeout=1500) or "")[:2000].lower()
    except Exception:
        body = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    for code in _TERMINAL_ACCOUNT_ERRORS:
        if code in body or code in url:
            return code
    return None


def assert_account_alive(page, where: str) -> None:
    """若页面显示账号已废,raise AccountDeactivated 立即终结。"""
    code = detect_account_deactivated(page)
    if code:
        try:
            safe_screenshot(page, SCREENSHOT_DIR / f"oauth_dead_{where}.png")
        except Exception:
            pass
        raise AccountDeactivated(
            f"账号已被 OpenAI 停用({code}@{where})— reauth 无意义,请删除"
        )


def _login_form_walk(page, email: str, password: str, mail_client, mail_baseline_id: int) -> None:
    """走 auth.openai.com /log-in 表单:邮箱 → 密码 → (可选)邮件 OTP。"""
    # email
    try:
        for attempt in range(2):
            ei = page.locator('input[name="email"], input[id="email-input"], input[id="email"]').first
            if not ei.is_visible(timeout=5000):
                break
            ei.fill(email)
            time.sleep(0.5)
            click_primary_button(page, ei, ["Continue", "继续"])
            time.sleep(3)
            if not is_google_redirect(page):
                break
            logger.warning("[oauth] 邮箱步骤误跳 Google,重试 (attempt %d)", attempt + 1)
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
    except Exception as exc:
        logger.debug("[oauth] 邮箱表单异常: %s", exc)

    # password
    try:
        for attempt in range(2):
            pi = page.locator('input[name="password"], input[type="password"]').first
            if not pi.is_visible(timeout=5000):
                break
            pi.fill(password)
            time.sleep(0.5)
            click_primary_button(page, pi, ["Continue", "继续", "Log in"])
            time.sleep(5)
            if not is_google_redirect(page):
                break
            logger.warning("[oauth] 密码步骤误跳 Google,重试 (attempt %d)", attempt + 1)
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
    except Exception as exc:
        logger.debug("[oauth] 密码表单异常: %s", exc)

    # OAuth 登录可能要求"新设备 OTP",从邮件取
    try:
        ci = page.locator(
            'input[name="code"], input[autocomplete*="one-time-code" i], '
            'input[placeholder*="验证码"], input[inputmode="numeric"]',
        ).first
        if ci.is_visible(timeout=5000):
            logger.info("[oauth] OAuth 要求 OTP,等待邮件 (after_id=%d)", mail_baseline_id)
            _, otp = mail_client.wait_for_otp(
                email, after_id=mail_baseline_id, timeout=EMAIL_POLL_TIMEOUT,
            )
            type_otp_code(page, ci, otp)
            page.locator('button:has-text("Continue"), button:has-text("继续"), button[type="submit"]').first.click()
            time.sleep(5)
    except Exception as exc:
        logger.debug("[oauth] OAuth OTP 表单异常(无要求): %s", exc)


def _login_form_walk_email_only(page, email: str, mail_client, mail_baseline_id: int) -> None:
    """走 auth.openai.com /log-in 表单 — 只用 email + 邮件 OTP,不需要密码。

    流程:
    1) 填 email → Continue
    2) 等下一步出现:可能直接是 code 输入框(理想),也可能是 password 页
    3) 若到了 password 页,找「Continue with code」/「Use a temporary code」之类的按钮切换到 OTP
    4) 拿 cloud-mail OTP 填进去 → Continue

    任何一步失败都抛 OAuthFailed,带明确原因。
    """
    # 1) 填 email + 继续
    try:
        for attempt in range(2):
            ei = page.locator('input[name="email"], input[id="email-input"], input[id="email"]').first
            if not ei.is_visible(timeout=5000):
                break
            ei.fill(email)
            time.sleep(0.5)
            click_primary_button(page, ei, ["Continue", "继续"])
            time.sleep(3)
            if not is_google_redirect(page):
                break
            logger.warning("[oauth-email-only] 邮箱步骤误跳 Google,重试 (attempt %d)", attempt + 1)
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
    except Exception as exc:
        raise OAuthFailed(f"填 email 阶段异常: {exc}") from exc

    # 2) 等下一步 — code 输入框直接出现 = 理想路径
    code_locator = 'input[name="code"], input[autocomplete="one-time-code"]'
    pwd_locator = 'input[name="password"], input[type="password"]'

    code_ready = False
    try:
        ci = page.locator(code_locator).first
        if ci.is_visible(timeout=8000):
            code_ready = True
            logger.info("[oauth-email-only] 邮箱直达 OTP 步骤(无密码页)")
    except Exception:
        pass

    # 3) 没直接到 code 步,看是否到了 password 页 — 找「用验证码登录」的切换按钮
    if not code_ready:
        try:
            pi = page.locator(pwd_locator).first
            if pi.is_visible(timeout=2000):
                logger.info("[oauth-email-only] 进了密码页 — 找「用验证码登录」按钮")
                # 多种文案 / 不同 UI 版本
                switch_selectors = [
                    # 当前 OpenAI 的真实文案(2026-05 实测)
                    'button:has-text("Log in with a one-time code")',
                    'a:has-text("Log in with a one-time code")',
                    '*[role="button"]:has-text("Log in with a one-time code")',
                    # 历史 / 其它语言 / 兼容性兜底
                    'button:has-text("Continue with code")',
                    'button:has-text("Use a one-time code")',
                    'button:has-text("Send a code")',
                    'button:has-text("Email a code")',
                    'a:has-text("Continue with code")',
                    'a:has-text("Use a one-time code")',
                    'a:has-text("Send a code")',
                    'button:has-text("使用验证码")',
                    'button:has-text("用验证码登录")',
                    'button:has-text("一次性")',
                    'a:has-text("使用验证码")',
                    'a:has-text("用验证码登录")',
                    'a:has-text("一次性")',
                    # 兜底:任何含 "one-time" 的可点元素
                    'button:has-text("one-time")',
                    'a:has-text("one-time")',
                    '[data-testid*="code-login"]',
                    '[data-testid*="passwordless"]',
                ]
                clicked = False
                for sel in switch_selectors:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=500):
                            try:
                                btn.scroll_into_view_if_needed(timeout=2000)
                            except Exception:
                                pass
                            btn.click()
                            clicked = True
                            logger.info("[oauth-email-only] 点了切换按钮 sel=%s", sel)
                            time.sleep(3)
                            break
                    except Exception:
                        continue
                if not clicked:
                    safe_screenshot(page, SCREENSHOT_DIR / "oauth_email_only_no_switch.png")
                    raise OAuthFailed(
                        "该账号要求密码登录,且页面没有「用验证码」入口 — "
                        "请改用「待办」页的「继续验证」(需要密码)或手动导入 token。"
                    )
                # 切换后再等 code 输入框
                ci = page.locator(code_locator).first
                if ci.is_visible(timeout=10000):
                    code_ready = True
        except OAuthFailed:
            raise
        except Exception as exc:
            raise OAuthFailed(f"识别登录步骤失败: {exc}") from exc

    if not code_ready:
        safe_screenshot(page, SCREENSHOT_DIR / "oauth_email_only_no_code_step.png")
        raise OAuthFailed("填 email 后既没出 code 框也没出 password 页 — 页面可能 stalled,见截图")

    # 4) 等邮件 OTP 并填
    logger.info("[oauth-email-only] 等待 cloud-mail OTP (after_id=%d)", mail_baseline_id)
    try:
        _, otp = mail_client.wait_for_otp(
            email, after_id=mail_baseline_id, timeout=EMAIL_POLL_TIMEOUT,
        )
    except Exception as exc:
        raise OAuthFailed(f"cloud-mail 取 OTP 超时: {exc}") from exc

    try:
        ci = page.locator(code_locator).first
        type_otp_code(page, ci, otp)
        page.locator(
            'button:has-text("Continue"), button:has-text("继续"), button[type="submit"]',
        ).first.click()
        time.sleep(5)
    except Exception as exc:
        raise OAuthFailed(f"填 OTP 失败: {exc}") from exc


_PHONE_INPUT_SELECTORS = (
    'input[type="tel"]',
    'input[inputmode="tel"]',
    'input[autocomplete*="tel" i]',
    'input[autocomplete="phone"]',
    'input[name*="phone" i]',
    'input[id*="phone" i]',
    'input[data-testid*="phone" i]',
    'input[placeholder*="phone" i]',
    'input[placeholder*="number" i]',
    'input[aria-label*="phone" i]',
    # intl-tel-input(常见 phone 库)生成的 input 既无 type=tel 也无 name=phone
    '.iti input',
    '.iti__tel-input',
    # 兜底:numeric inputmode 的输入框
    'input[inputmode="numeric"]',
)


def _find_phone_input(page, timeout_each: int = 800):
    """多选择器逐个试,返回第一个 visible+editable 的 phone input;找不到返回 None。"""
    for sel in _PHONE_INPUT_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=timeout_each) and el.is_editable(timeout=300):
                logger.info("[oauth] phone 输入框找到 sel=%s", sel)
                return el
        except Exception:
            continue
    return None


def _fill_otp_smart(page, otp: str) -> bool:
    """智能 OTP 填写,处理两种 OpenAI UI:
       (a) 单 input(autocomplete=one-time-code,1 个框输全部 6 位)
       (b) 6 个独立 cell(每个 maxlength=1,autocomplete=one-time-code)

    策略:先数 visible OTP 候选 input,数量 >=6 → 按 cell 填;否则当作单 input。
    成功返回 True,失败 False。
    """
    otp = (otp or "").strip()
    if not otp:
        return False
    cell_selectors = (
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"][maxlength="1"]',
        'input[maxlength="1"][type="text"]',
        'input[maxlength="1"][type="tel"]',
    )
    # 数 visible cells
    visible_cells = []
    for sel in cell_selectors:
        try:
            cnt = page.locator(sel).count()
            if cnt >= 6:
                cells = page.locator(sel)
                for i in range(min(cnt, 6)):
                    cell = cells.nth(i)
                    try:
                        if cell.is_visible(timeout=300):
                            visible_cells.append(cell)
                    except Exception:
                        pass
                if len(visible_cells) >= len(otp):
                    break
                visible_cells = []
        except Exception:
            continue

    if len(visible_cells) >= len(otp):
        logger.info("[oauth] OTP 6-cell 模式 cells=%d", len(visible_cells))
        try:
            for i, ch in enumerate(otp):
                visible_cells[i].fill(ch, timeout=3000)
                time.sleep(0.05)
            return True
        except Exception as exc:
            logger.warning("[oauth] OTP 6-cell 填写失败: %s — 回退到单 input 模式", exc)

    # 单 input 兜底
    single_selectors = (
        'input[autocomplete="one-time-code"]',
        'input[name="code"]',
        'input[name="otp"]',
        'input[id*="code" i]',
        'input[data-testid*="code" i]',
        'input[placeholder*="code" i]',
        'input[inputmode="numeric"]',
    )
    for sel in single_selectors:
        try:
            el = page.locator(sel).first
            if not el.is_visible(timeout=1500):
                continue
            try:
                el.fill(otp, timeout=3000)
                logger.info("[oauth] OTP 单 input 模式 sel=%s", sel)
                return True
            except Exception:
                # fill 失败 → 试 press_sequentially(逐字符按键)
                try:
                    el.click(timeout=2000)
                    page.keyboard.type(otp, delay=50)
                    logger.info("[oauth] OTP keyboard.type 模式 sel=%s", sel)
                    return True
                except Exception as exc2:
                    logger.debug("[oauth] OTP fill+type 都失败 sel=%s: %s", sel, exc2)
        except Exception:
            continue
    return False


_PHONE_PAGE_URL_MARKERS = ("add-phone", "verify-phone", "phone-number", "phone-verification", "/phone")
# OpenAI 把号验证通过后 URL 会跳到这些 — 命中即 phone gate 已过,不必再等 _PHONE 标记消失
_POST_PHONE_SUCCESS_MARKERS = ("/consent", "/authorize", "/callback", "chatgpt.com", "localhost:1455")
# Body 文本里只有 consent / 后续页才会出现的固定字串 — 用作 URL fallback 信号
# (Playwright page.url 偶发 stale 不更新,body 渲染却已是新页面 — 用户实测撞过两次)
_POST_PHONE_SUCCESS_BODY_MARKERS = (
    "sign in to codex",            # consent 页大标题
    "by continuing, chatgpt",      # consent 页副标题
    "codex will not receive",      # consent 页正文
    "登录 codex",
    "继续即表示",
)

# OpenAI OAuth 出错页 body 标记 — 命中即重跑 auth_url(workspace 还没在 backend 端 propagate
# 完, 等几秒重试通常就过). 已知触发: 刚注册账号第一次 OAuth, OAuth session 端 workspaces=[]
# → /authorize 拒绝 → 渲染 "Oops, an error occurred (no_valid_organizations)" 错误页.
_OAUTH_ERROR_BODY_MARKERS = (
    "no_valid_organizations",
    "an error occurred during authentication",
    "oops, an error occurred",
)


def _detect_oauth_error(page) -> str | None:
    """检测 consent 页是否渲染了 OpenAI OAuth 错误页。命中返错误关键词,否则 None。"""
    try:
        body = page.locator("body").inner_text(timeout=1000).lower()
    except Exception:
        return None
    for marker in _OAUTH_ERROR_BODY_MARKERS:
        if marker in body:
            return marker
    return None


def _is_on_phone_url(page) -> bool:
    """页面 URL 是否在 phone gate 各阶段(包括 verifying 中间页)。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    return any(m in url for m in _PHONE_PAGE_URL_MARKERS)


def _is_post_phone_success(url: str) -> bool:
    """URL 命中 success 标记 — phone gate 已通过,可以继续 consent / auth_code 流程。"""
    return any(m in (url or "").lower() for m in _POST_PHONE_SUCCESS_MARKERS)


# OpenAI 在 phone-verification 页明确拒收时常见的 body 文本 — 命中即立即放弃,
# 不必等满 180s。覆盖中英文 + 关键变体。
# 慎选:必须是只在"拒收"状态出现、不会在 verifying loading 状态出现的字串。
_PHONE_REJECT_BODY_MARKERS = (
    "couldn't be verified",
    "could not be verified",
    "couldn't verify your phone",
    "could not verify your phone",
    "phone number could not",
    "verification failed",
    "verification could not be completed",
    "isn't supported",
    "is not supported",
    "not supported in your",
    "this phone number isn't eligible",
    "phone number is not eligible",
    "wrong code",
    "incorrect code",
    "code is invalid",
    "invalid code",
    "try a different phone",
    "try a different number",
    "use a different phone",
    "无法验证",
    "验证失败",
    "号码无效",
    "不支持",
    "请使用其他",
)


def _phone_page_body_state(page) -> tuple[str, str]:
    """读 phone-verification 页 body, 返回 (state, body_excerpt).

    state:
      "rejected"  — body 命中 reject marker, 调用方应立即放弃 + 退款
      "verifying" — 还在 loading / 没有明确错误, 调用方继续等
      "unknown"   — body 读不到 (网络抖动 / page 关了)
    """
    try:
        body = page.locator("body").inner_text(timeout=1000)
    except Exception:
        return "unknown", ""
    body_lower = body.lower()
    for marker in _PHONE_REJECT_BODY_MARKERS:
        if marker.lower() in body_lower:
            return "rejected", body[:300].replace("\n", " ")
    return "verifying", body[:300].replace("\n", " ")


def _live_url(page) -> str:
    """拿真实当前 URL — page.url 偶发 stale, fallback 到 page.evaluate(location.href)。

    Playwright sync API 实测过这个:页面已渲染新组件 + body 已更新,但 page.url
    属性还指向上一个 URL。原因不明(可能跟 SPA route + Playwright 内部缓存有关)。
    page.evaluate 直接问浏览器 window.location.href, 永远是真值.

    例外:连接被拒(localhost:1455 没人监听)时 evaluate 返 'chrome-error://...',
    但 page.url 仍是原始的 callback URL 含 code= — 这种情况优先 page.url。
    """
    try:
        u1 = (page.url or "").lower()
    except Exception:
        u1 = ""
    try:
        u2 = str(page.evaluate("() => window.location.href") or "").lower()
    except Exception:
        u2 = ""
    # evaluate 返 chrome-error/about:blank/data: 时退回 page.url(可能含真实跳转 URL)
    if u2.startswith(("chrome-error", "about:", "data:")):
        return u1 or u2
    return u2 or u1


def _check_body_success(page) -> bool:
    """读 body 是否含 consent/post-phone 页固定字串 — URL stale 时的兜底信号。"""
    try:
        body = page.locator("body").inner_text(timeout=1000).lower()
    except Exception:
        return False
    return any(m in body for m in _POST_PHONE_SUCCESS_BODY_MARKERS)


def _wait_phone_clear(page, max_seconds: int = 150) -> bool:
    """提交 OTP 后轮询 URL + body,直到跳出 phone 页 OR 检测到拒收。

    成功条件 — 命中任一即返 True:
      A. URL (用 _live_url 双源采集) 命中 _POST_PHONE_SUCCESS_MARKERS
      B. URL 离开 phone-verification 标记
      C. **Body 命中 _POST_PHONE_SUCCESS_BODY_MARKERS** ← URL stale 时的 fallback
         实测 Playwright page.url 会卡在 phone-verification 整 180s, 但 body 已渲染
         consent 页面 ("Sign in to Codex with ChatGPT...")。Body 检查 5s 一次, 命中
         即认为成功 — 用户实测 vietnam 号撞过 2 次, $0.26 都是这个 bug 吃掉.
    失败立即返回 False:
      - body 命中 _PHONE_REJECT_BODY_MARKERS (如 "couldn't be verified")
    超时返回 False — OpenAI fraud check 真的慢 / 卡死中间状态。
    """
    def _poll() -> tuple[str, bool]:
        url = _live_url(page)
        if _is_post_phone_success(url):
            return url, True
        on_phone = any(m in url for m in _PHONE_PAGE_URL_MARKERS)
        return url, (not on_phone)

    deadline = time.time() + max_seconds
    last_url = ""
    last_log = 0.0
    last_body_check = 0.0
    log_interval = 10
    body_check_interval = 5  # 5s 一次 body 检查
    while time.time() < deadline:
        url, cleared = _poll()
        if url and url != last_url:
            logger.info("[oauth] _wait_phone_clear url=%s", url)
            last_url = url
        elif time.time() - last_log > log_interval:
            remaining = int(deadline - time.time())
            logger.info("[oauth] _wait_phone_clear 等 phone 跳走... 已等 %ds (deadline 在 %ds 后)",
                        max_seconds - remaining, remaining)
            last_log = time.time()
        if cleared:
            logger.info("[oauth] phone 已通过 url=%s", url)
            return True
        # body 检查 — success 与 reject 同时检测(每 5s 一次, 避免 inner_text 频繁请求)
        if time.time() - last_body_check >= body_check_interval:
            last_body_check = time.time()
            # 1) success 标记 — URL stale 兜底, 这是最关键的修复
            if _check_body_success(page):
                logger.info("[oauth] phone 已通过 — body 命中 consent 文本 (URL=%s 可能 stale)",
                            _live_url(page))
                return True
            # 2) reject 标记 — fail-fast
            state, excerpt = _phone_page_body_state(page)
            if state == "rejected":
                logger.warning(
                    "[oauth] _wait_phone_clear body 命中拒收标记 — 立即放弃, body=%r",
                    excerpt,
                )
                safe_screenshot(page, SCREENSHOT_DIR / "oauth_phone_rejected_body.png")
                return False
        time.sleep(1)

    # 主超时后再给 30s 宽限 — 兜底边缘 race(OpenAI fraud check 慢一拍才跳)
    grace_seconds = 30
    grace_end = time.time() + grace_seconds
    grace_body_check = 0.0
    logger.info("[oauth] _wait_phone_clear 主超时, 进入 %ds 宽限期", grace_seconds)
    while time.time() < grace_end:
        url, cleared = _poll()
        if url and url != last_url:
            logger.info("[oauth] _wait_phone_clear (grace) url=%s", url)
            last_url = url
        if cleared:
            logger.info("[oauth] phone 已通过 — 宽限期命中 url=%s", url)
            return True
        # grace 期 body 也要查 — URL stale 时这是唯一信号
        if time.time() - grace_body_check >= 3:
            grace_body_check = time.time()
            if _check_body_success(page):
                logger.info("[oauth] phone 已通过 — 宽限期 body 命中 consent 文本")
                return True
        time.sleep(1)

    # 超时前最后再 body 检测 + 读真实 URL + 截图 — 留全部诊断证据
    final_body_success = _check_body_success(page)
    final_url = _live_url(page)
    if final_body_success or _is_post_phone_success(final_url):
        logger.info(
            "[oauth] phone 已通过 — 全部超时前最终检查救回 (url=%s body_success=%s)",
            final_url, final_body_success,
        )
        return True
    state, excerpt = _phone_page_body_state(page)
    safe_screenshot(page, SCREENSHOT_DIR / "oauth_phone_timeout_body.png")
    logger.warning(
        "[oauth] _wait_phone_clear 全部超时 (180s), live_url=%s page.url=%s body_state=%s body=%r",
        final_url, page.url, state, excerpt,
    )
    return False


def _solve_phone_gate(page) -> bool:
    """检测到 add-phone 页时调 active SMS provider 取号 → 填表 → 取 OTP → 提交。

    成功返回 True;失败 raise RegisterBlocked(is_phone=True) 让上层标记阻断。
    顺序关键:先找输入框,再买号 — 否则 input 找不到会浪费余额。
    provider 由 panel 配置(5sim / hero-sms / ...),通过 sms.get_active_provider 工厂拿。
    """
    # 入口先截图 — 任何买号操作前留影,失败时方便诊断
    safe_screenshot(page, SCREENSHOT_DIR / "oauth_phone_gate_entry.png")
    logger.info("[oauth] phone gate entry url=%s", page.url)

    cfg = get_sms_config()
    try:
        provider = sms_mod.get_active_provider(cfg)
    except sms_mod.SmsConfigMissing as exc:
        raise RegisterBlocked("oauth_phone_gate", str(exc), is_phone=True) from exc

    country = cfg.get("country") or provider.DEFAULT_COUNTRY
    operator = cfg.get("operator") or provider.DEFAULT_OPERATOR
    service = cfg.get("service") or provider.DEFAULT_SERVICE
    logger.info(
        "[oauth] SMS provider=%s 实际使用配置 country=%s operator=%s service=%s",
        provider.PROVIDER_NAME, country, operator, service,
    )

    # === 关键:先确认 input 存在,再花钱买号 ===
    tel = _find_phone_input(page, timeout_each=1500)
    if tel is None:
        screenshot = SCREENSHOT_DIR / "oauth_phone_gate_no_input.png"
        safe_screenshot(page, screenshot)
        body_excerpt = ""
        try:
            body_excerpt = page.locator("body").inner_text(timeout=1500)[:300].replace("\n", " ")
        except Exception:
            pass
        raise RegisterBlocked(
            "oauth_phone_gate",
            f"phone 输入框找不到 (url={page.url}). 截图: {screenshot.name}. body: {body_excerpt!r}",
            is_phone=True,
        )

    # 虚拟号死号率高,允许多重试。超时用 ban(全额退 + provider 不再分同号);
    # 其它失败(SMS 还没到时的 fill 失败等)用 cancel — SMS 未到时也是全额退
    MAX_ATTEMPTS = 4
    OTP_WAIT_SECONDS = 90   # 1.5 min — 用户实测 90s 没到 = 死号 100%, 继续等纯浪费时间
    last_err: Exception | None = None
    spent_usd = 0.0
    used_order_ids: list[int] = []

    def _refund_via_cancel(order_id, price):
        """SMS 还没到时 cancel 全额退;减回 spent_usd。"""
        nonlocal spent_usd
        try:
            provider.cancel_order(order_id)
            spent_usd -= price
        except Exception as exc:
            logger.warning("[oauth] cancel 失败(可能 SMS 已到 → 不退): %s", exc)

    for attempt in range(MAX_ATTEMPTS):
        # 重试间隙先看 stop 信号(用户在死号循环里点 Stop)
        if is_stop_requested():
            raise BatchStopped(f"phone gate 重试间隙收到 stop 信号 (attempt={attempt+1}/{MAX_ATTEMPTS})")
        order = None
        try:
            tel_now = _find_phone_input(page, timeout_each=1500) if attempt > 0 else tel
            if tel_now is None:
                logger.warning("[oauth] 第 %d 次重试时 phone input 不可见 — 放弃", attempt + 1)
                safe_screenshot(page, SCREENSHOT_DIR / f"oauth_phone_retry_no_input_{attempt+1}.png")
                last_err = RegisterBlocked("oauth_phone_gate", "重试时 phone input 已消失", is_phone=True)
                break

            order = provider.buy_activation(country=country, operator=operator, product=service)
            if order.id in used_order_ids:
                logger.warning("[oauth] %s 给了重复号 id=%d phone=%s,跳过",
                               provider.PROVIDER_NAME, order.id, order.phone)
                provider.cancel_order(order.id)
                last_err = RegisterBlocked("oauth_phone_gate",
                                           f"{provider.PROVIDER_NAME} 反复给同一死号", is_phone=True)
                continue
            used_order_ids.append(order.id)
            spent_usd += order.price
            logger.info("[oauth] attempt=%d/%d 累计 %s 花费 $%.4f",
                        attempt + 1, MAX_ATTEMPTS, provider.PROVIDER_NAME, spent_usd)
            phone_full = order.phone or ""

            try:
                tel_now.fill("", timeout=5000)
                time.sleep(0.3)
                tel_now.fill(phone_full, timeout=5000)
            except Exception as fill_exc:
                logger.warning("[oauth] phone fill 异常 attempt=%d,cancel 退款: %s", attempt + 1, fill_exc)
                _refund_via_cancel(order.id, order.price)
                last_err = RegisterBlocked("oauth_phone_gate", f"phone fill 失败: {fill_exc}", is_phone=True)
                continue

            time.sleep(0.5)
            click_primary_button(page, tel_now, ["Continue", "Send code", "继续", "发送验证码", "Next"])
            time.sleep(5)

            otp_appeared = False
            for _ in range(12):
                try:
                    if page.locator(
                        'input[autocomplete="one-time-code"], input[name="code"], input[inputmode="numeric"]'
                    ).first.is_visible(timeout=1000):
                        otp_appeared = True
                        break
                except Exception:
                    pass
                time.sleep(1)
            if not otp_appeared:
                logger.warning("[oauth] 提交 phone 后未出现 OTP 输入框 — 可能号被拒")
                safe_screenshot(page, SCREENSHOT_DIR / f"oauth_phone_no_otp_{attempt+1}.png")
                _refund_via_cancel(order.id, order.price)
                last_err = RegisterBlocked("oauth_phone_gate", "号被 OpenAI 拒收(没出 OTP 框)", is_phone=True)
                continue

            otp = provider.wait_for_otp(
                order_id=order.id, timeout=OTP_WAIT_SECONDS, should_stop=is_stop_requested,
            )

            if not _fill_otp_smart(page, otp):
                logger.warning("[oauth] OTP 填写失败 attempt=%d (SMS 已到 — cancel 不退款)", attempt + 1)
                safe_screenshot(page, SCREENSHOT_DIR / f"oauth_otp_fill_failed_{attempt+1}.png")
                try: provider.cancel_order(order.id)
                except Exception: pass
                last_err = RegisterBlocked("oauth_phone_gate", "OTP 填写失败 (selector 都没匹配上)", is_phone=True)
                continue

            time.sleep(0.5)
            try:
                btn = page.locator(
                    'button:has-text("Verify"), button:has-text("Continue"),'
                    ' button:has-text("继续"), button:has-text("验证"),'
                    ' button[type="submit"]'
                ).first
                if btn.is_visible(timeout=2000):
                    btn.click(timeout=3000)
                    logger.info("[oauth] OTP 已点 Verify/Continue")
            except Exception:
                logger.debug("[oauth] OTP 可能 6-cell 自动提交,无 Verify 按钮")

            safe_screenshot(page, SCREENSHOT_DIR / f"oauth_otp_submitted_{attempt+1}.png")

            unlocked = _wait_phone_clear(page)  # 默认 150s + 30s grace, 见函数 doc
            if unlocked:
                provider.finish_order(order.id)
                logger.info("[oauth] phone gate 解锁成功 attempt=%d order_id=%d", attempt + 1, order.id)
                return True

            err_excerpt = ""
            try:
                err_excerpt = page.locator("body").inner_text(timeout=1500)[:300].replace("\n", " ")
            except Exception:
                pass
            logger.warning(
                "[oauth] ⚠ OTP 提交 + 等 180s (150 主 + 30 宽限) 后仍在 phone 页 — OpenAI 拒收号 country=%s operator=%s url=%s body=%r",
                order.country, order.operator, page.url, err_excerpt,
            )
            safe_screenshot(page, SCREENSHOT_DIR / f"oauth_phone_after_otp_rejected_{attempt+1}.png")
            try: provider.cancel_order(order.id)
            except Exception: pass
            last_err = RegisterBlocked(
                "oauth_phone_gate",
                f"OpenAI 拒收 {order.country}/{order.operator} 号 (OTP 验证后 180s 仍在 add-phone)",
                is_phone=True,
            )
            break

        except sms_mod.SmsAborted as exc:
            if order is not None:
                try:
                    provider.cancel_order(order.id)
                    spent_usd -= order.price
                    logger.warning("[oauth] stop 触发 — cancel 退款 order_id=%d 累计 $%.4f", order.id, spent_usd)
                except Exception as cancel_exc:
                    logger.warning("[oauth] stop 触发但 cancel 失败: %s", cancel_exc)
            raise BatchStopped(f"phone gate 等 OTP 时被中断: {exc}") from exc
        except sms_mod.SmsBuyFailed as exc:
            last_err = exc
            logger.warning("[oauth] %s 下单失败 attempt=%d/%d: %s",
                           provider.PROVIDER_NAME, attempt + 1, MAX_ATTEMPTS, exc)
            continue
        except sms_mod.SmsTimeout as exc:
            last_err = exc
            if order is not None:
                try:
                    provider.ban_order(order.id)
                    spent_usd -= order.price
                    logger.warning(
                        "[oauth] %s 等 OTP 超时 attempt=%d/%d %ds 死号 ban+退款 — 累计 $%.4f",
                        provider.PROVIDER_NAME, attempt + 1, MAX_ATTEMPTS, OTP_WAIT_SECONDS, spent_usd,
                    )
                except Exception as ban_exc:
                    logger.warning("[oauth] ban 失败,fallback cancel: %s", ban_exc)
                    try: provider.cancel_order(order.id)
                    except Exception: pass
            continue
        except RegisterBlocked:
            if order is not None:
                try: provider.cancel_order(order.id)
                except Exception: pass
            raise
        except Exception as exc:
            last_err = exc
            if order is not None:
                try: provider.cancel_order(order.id)
                except Exception: pass
            logger.exception("[oauth] phone gate 异常 attempt=%d", attempt + 1)
            continue

    raise RegisterBlocked(
        "oauth_phone_gate",
        f"phone 验证失败 (provider={provider.PROVIDER_NAME} {MAX_ATTEMPTS} 次已用尽,累计净花费 ${spent_usd:.4f}): {last_err}",
        is_phone=True,
    )


# consent 页 Continue 按钮 — 沿用过去半年验证过的 selector 方案。
# 用 Playwright 单 locator 多文本(逗号 = OR), .first 拿第一个匹配, has-text 自动忽略
# 大小写 + 空白. 实测在 team 路径上稳定工作, 不要再过度设计.
#
# 之前自己写了 12 个 :not(:has-text("with")) 嵌套, Playwright 对 :not(:has-text())
# 支持有限, 经常匹配到非 submit 的隐藏 button — 看似点击成功但 form 没提交.
_CONSENT_BUTTON_LOCATOR = (
    'button:has-text("继续"), button:has-text("Continue"), '
    'button:has-text("Allow"), button:has-text("Authorize"), '
    'button:has-text("授权")'
)


def _click_consent_button(page, *, after_workspace_pick: bool = False) -> bool:
    """点 consent 页面的 Continue/Authorize 按钮,返回是否点中了。

    简单做法:单 locator + 多文本 OR + .first.click() + sleep(5).
    Playwright .click() 自带 actionability 等待, sleep 5s 给浏览器跑完
    consent → /authorize → /callback → localhost:1455 整条 redirect 链.
    """
    try:
        b = page.locator(_CONSENT_BUTTON_LOCATOR).first
        if b.is_visible(timeout=5000):
            b.click(timeout=5000)
            logger.info("[oauth] consent %s点击成功", "(workspace 选完)" if after_workspace_pick else "")
            time.sleep(5)
            return True
    except Exception as exc:
        logger.warning("[oauth] consent 按钮点击失败: %s", exc)
    return False


def _consent_step(page) -> bool:
    """在 consent 页面尝试一次:先选 Personal workspace(若有 picker),再点 Continue。

    返回 True 表示这一轮做了有效点击(下一轮可能进入下一步或直接拿 auth_code)。
    """
    # 1) workspace picker — 部分账号 consent 前先弹 Personal/Team 选择
    try:
        body = page.inner_text("body")[:1000]
        if any(kw in body for kw in ("选择一个工作空间", "Select a workspace", "选择工作空间")):
            for sel in (
                'text=/^Personal$/',
                'text=/^个人$/',
                'button:has-text("Personal"):not(:has-text("ChatGPT"))',
                'button:has-text("个人")',
            ):
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=1500):
                        el.click(force=True)
                        logger.info("[oauth] 选 Personal workspace sel=%s", sel)
                        time.sleep(1)
                        _click_consent_button(page, after_workspace_pick=True)
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    # 2) 主路径 — 点 consent 的 Continue 按钮
    return _click_consent_button(page)


def fetch_personal_bundle(
    *,
    email: str,
    password: str | None = None,
    mail_client,
    session_token: str | None = None,
) -> dict:
    """跑一遍 personal codex OAuth,返回 bundle dict。失败抛 OAuthFailed。

    session_token: 注册阶段抽出的 chatgpt.com __Secure-next-auth.session-token。
    传入则注入到 chatgpt.com + auth.openai.com 双域,先 goto chatgpt.com 触发 silent step-0,
    然后 goto auth_url 时 OAuth backend 看到已有会话直接进 consent,跳过 /log-in →
    避开 add-phone 风控(刚注册的 personal 走 /log-in 大概率撞)。

    password=None → 走「邮箱 OTP-only」登录(对纯手动添加的号,只需要 email + cloud-mail)。
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    code_verifier, code_challenge = _pkce()
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(code_challenge, state)
    mail_baseline_id = mail_client.latest_mail_id(email)
    logger.info(
        "[oauth] 开始 OAuth %s (mail_baseline_id=%d, session_token=%s)",
        email, mail_baseline_id, "yes" if session_token else "no",
    )
    # 跟踪本次 OAuth 是否真实通过 5sim 付费(_solve_phone_gate finish_order 成功才算)。
    # 用 list 包一层让闭包可写。
    phone_paid_via_sms = [False]

    auth_code: list[str | None] = [None]

    proxy_session_id = make_proxy_session_id(prefix=email.split("@", 1)[0])
    proxy_opts = get_proxy_options(session_id=proxy_session_id)
    launch_kwargs = get_launch_options()
    if proxy_opts:
        launch_kwargs["proxy"] = proxy_opts
        logger.info("[oauth] 使用代理 session=%s server=%s", proxy_session_id, proxy_opts["server"])

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(**get_context_options())

        # 注:用 prompt=login 后, OpenAI 会强制走 /log-in 流程, session_token 注入和
        # silent step-0 都不再需要 (它们的目的是"绕过 /log-in", 但 prompt=login 直接
        # 让我们走标准登录路径 → 登录时 auth.openai.com 端会 mint workspace 进 OAuth session,
        # 根治 no_valid_organizations). session_token 不注, _silent_step0 不调.

        page = context.new_page()

        def _try_extract_code(url: str, source: str) -> bool:
            if not url or auth_code[0]:
                return False
            # 宽松匹配:只要 path 含 /auth/callback 且 query 含 code=,无论 host
            # (localhost / 127.0.0.1 / [::1] 都视作 callback)
            url_low = url.lower()
            if "/auth/callback" not in url_low:
                return False
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            code = qs.get("code", [None])[0]
            if code:
                auth_code[0] = code
                logger.info("[oauth] 捕获 auth_code (%s) url=%s", source, url[:120])
                return True
            return False

        def _on_request(request):
            try:
                _try_extract_code(request.url, "request")
            except Exception:
                pass

        def _on_requestfailed(request):
            # 连接被拒(localhost:1455 没监听 = ERR_CONNECTION_REFUSED)走这里
            # 这时 page 可能已渲染 chrome-error,但 request.url 仍是原始 callback URL
            try:
                _try_extract_code(request.url, "requestfailed")
            except Exception:
                pass

        def _on_response(response):
            try:
                _try_extract_code(response.url, "response")
            except Exception:
                pass

        def _on_framenav(frame):
            try:
                _try_extract_code(frame.url, "framenav")
            except Exception:
                pass

        page.on("request", _on_request)
        page.on("requestfailed", _on_requestfailed)
        page.on("response", _on_response)
        page.on("framenavigated", _on_framenav)

        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)

            # auth.openai.com 经常先显示 Cloudflare turnstile,等它过完再找登录表单
            # 否则 input[name=email] 一直找不到,会直接走到 stalled 错误
            from autofree.core.browser import wait_cloudflare
            cf_ok = wait_cloudflare(page, max_wait_seconds=90)
            if not cf_ok:
                logger.warning("[oauth] auth.openai.com Cloudflare 90s 内未通过 — 可能 IP 信誉问题")
            safe_screenshot(page, SCREENSHOT_DIR / "oauth_01_auth_page.png")

            # prompt=login 强制走 /log-in, 必然要填表
            if password:
                _login_form_walk(page, email, password, mail_client, mail_baseline_id)
            else:
                # email-only 模式:无密码,从 cloud-mail 取 OTP
                _login_form_walk_email_only(page, email, mail_client, mail_baseline_id)
            safe_screenshot(page, SCREENSHOT_DIR / "oauth_02_after_login.png")

            # 关键:登录后立即查账号是否已废 — 撞到 account_deactivated 等错误就直接抛,
            # 不进 phone gate(省 5sim)、不进 consent loop
            assert_account_alive(page, "post_login")

            # add-phone gate:先尝试 5sim 解锁,解不开才 raise RegisterBlocked
            if detect_phone_block(page):
                logger.info("[oauth] 检测到 phone gate,尝试 5sim 自动验证")
                _solve_phone_gate(page)
                # 走到这说明 5sim finish_order 成功(扣费已发生)
                phone_paid_via_sms[0] = True
                logger.info("[oauth] ✓ 标记 phone_verified — 5sim 已扣费,此号必须保留")
                safe_screenshot(page, SCREENSHOT_DIR / "oauth_02b_phone_done.png")

            assert_not_blocked(page, "oauth_post_login")
            assert_account_alive(page, "post_phone")

            # consent loop:最多 8 轮 — 沿用经验证过的成熟做法.
            # auth_code 主要靠 page.on(request/response/framenavigated) 三个 hook 抓取
            # localhost:1455/auth/callback?code=... 的 redirect; URL polling 用 _live_url
            # 兜底 (page.url 偶发 stale, 之前 phone gate 已踩过).
            for step in range(8):
                if auth_code[0]:
                    break
                # 1) 先用真值 URL 看一眼 — 可能已经跳到 localhost callback 了
                try:
                    _try_extract_code(_live_url(page), f"consent_{step}_url_poll")
                except Exception:
                    pass
                if auth_code[0]:
                    break
                # 2) consent 中也可能突然蹦出 phone gate(罕见)
                if detect_phone_block(page):
                    logger.info("[oauth] consent 中遇到 phone gate,尝试 5sim 解锁")
                    _solve_phone_gate(page)
                    phone_paid_via_sms[0] = True
                    logger.info("[oauth] ✓ 标记 phone_verified (consent 中)")
                assert_not_blocked(page, f"oauth_consent_{step}")
                assert_account_alive(page, f"consent_{step}")
                acted = _consent_step(page)
                safe_screenshot(page, SCREENSHOT_DIR / f"oauth_03_consent_{step + 1}.png")
                if not acted:
                    # 没找到按钮 = 已经跳走了, 让外层 callback 等待兜
                    logger.info("[oauth] consent step=%d 未见 Continue, 跳出 loop url=%s",
                                step + 1, _live_url(page))
                    break
                # 4) 点完后再用真值 URL 抠一次 code
                try:
                    _try_extract_code(_live_url(page), f"consent_{step}_post_click")
                except Exception:
                    pass
                if auth_code[0]:
                    break
                logger.info("[oauth] consent step=%d 后 url=%s", step + 1, _live_url(page))

            # 等 callback 最多 30s — _live_url + 事件 hook 双保险
            if not auth_code[0]:
                logger.info("[oauth] consent 后等 callback 最多 30s")
                deadline = time.time() + 30
                while time.time() < deadline and not auth_code[0]:
                    try:
                        _try_extract_code(_live_url(page), "tail_url_poll")
                    except Exception:
                        pass
                    time.sleep(1)

            safe_screenshot(page, SCREENSHOT_DIR / "oauth_04_final.png")
        except Exception as _exc:
            # 任何异常出来都把 phone_paid 标记带上,不让 5sim 付费记录在异常路径丢失
            if phone_paid_via_sms[0]:
                try: _exc.phone_paid_via_sms = True  # type: ignore[attr-defined]
                except Exception: pass
            raise
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if not auth_code[0]:
        # 重要:即使 OAuth 拿不到 code,只要 5sim 这次扣过费,也要把这个状态告诉调用方,
        # 让 PendingAccount.phone_verified=True 标记,resume 时就不会再烧 5sim
        if phone_paid_via_sms[0]:
            exc = OAuthFailed(f"未捕获到 auth_code (page may have stalled). 看截图 {SCREENSHOT_DIR}/")
            exc.phone_paid_via_sms = True  # type: ignore[attr-defined]
            raise exc
        raise OAuthFailed(f"未捕获到 auth_code (page may have stalled). 看截图 {SCREENSHOT_DIR}/")

    bundle = _exchange_code(auth_code[0], code_verifier, fallback_email=email)
    bundle["phone_verified"] = phone_paid_via_sms[0]
    return bundle
