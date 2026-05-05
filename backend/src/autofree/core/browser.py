"""Playwright Chromium 启动 + 反爬基础。

freegen PoC 不带代理。正式版会从这里挂代理池。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


_PHONE_URL_HINTS = ("verify-phone", "add-phone", "/phone", "phone_verification", "phone-number")
_PHONE_TEXT_HINTS = (
    "verify your phone", "add your phone", "verify phone",
    "verification code to your phone", "add a phone number", "add a phone",
    "enter your phone", "phone verification", "we'll text you",
    "请输入手机号", "手机号码", "验证手机", "添加手机",
)
_DUPLICATE_TEXT_HINTS = (
    "already have an account", "already exists", "already been used",
    "this user already exists", "please use a different email", "different email",
    "email is already taken", "account with this email",
    "该邮箱已被使用", "邮箱已存在", "请使用其他邮箱", "电子邮件已被使用",
)


def get_launch_options() -> dict:
    """统一的 Chromium 启动参数。

    默认 headless=False 与 autoteam 一致(headless 会被 Cloudflare turnstile 抓)。
    Linux 上需要 DISPLAY 环境变量(Xvfb 或本地桌面);AutoFree Docker 镜像装了 Xvfb 并通过
    xvfb-run 拉起 uvicorn,所以默认就有 DISPLAY 可用。
    若你确认要 headless,设 FREEGEN_HEADLESS=1 或 PLAYWRIGHT_HEADLESS=true。
    """
    headless_env = (
        os.environ.get("FREEGEN_HEADLESS")
        or os.environ.get("PLAYWRIGHT_HEADLESS")
        or "0"
    ).strip().lower()
    headless = headless_env in ("1", "true", "yes", "on")
    return {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def get_context_options() -> dict:
    return {
        "viewport": {"width": 1280, "height": 800},
        "user_agent": DEFAULT_USER_AGENT,
    }


def safe_screenshot(page, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=True)
    except Exception as exc:
        logger.debug("[browser] 截图失败 %s: %s", path, exc)


def page_excerpt(page, limit: int = 240) -> str:
    try:
        return page.locator("body").inner_text(timeout=1500)[:limit].replace("\n", " ")
    except Exception:
        return ""


# 明确"已通过 phone"的 URL 标记 — 优先级高于 body 文本检查,避免 consent 页的
# 其它 phone 字样导致误判
_POST_PHONE_URL_MARKERS = (
    "/consent",
    "/callback",
    "/authorize",
    "/sign-in-with-chatgpt/",
    "chatgpt.com/",
    "localhost:1455",
)


def detect_phone_block(page) -> bool:
    try:
        url = (page.url or "").lower()
        # 1) 已进 consent / callback → 一定不是 phone gate(实测过坑:consent 页 body
        #    里有"phone"字样 + 残留隐藏 tel input,会被误判)
        if any(s in url for s in _POST_PHONE_URL_MARKERS):
            return False
        # 2) URL 显式 phone 提示 → True
        if any(h in url for h in _PHONE_URL_HINTS):
            return True
        # 3) body + 可见 tel input
        body = page.inner_text("body")[:1500].lower()
        if not any(h in body for h in _PHONE_TEXT_HINTS):
            return False
        try:
            tel = page.locator('input[type="tel"], input[name*="phone" i], input[autocomplete*="tel" i]').first
            return tel.is_visible(timeout=500)
        except Exception:
            return False
    except Exception:
        return False


def detect_duplicate(page) -> bool:
    try:
        body = page.inner_text("body")[:1500].lower()
        return any(h in body for h in _DUPLICATE_TEXT_HINTS)
    except Exception:
        return False


def assert_not_blocked(page, step: str) -> None:
    """任何关键提交后调用,撞 add-phone / duplicate 立即 raise。"""
    from autofree.core.errors import RegisterBlocked

    if detect_phone_block(page):
        raise RegisterBlocked(step, "add-phone 手机验证", is_phone=True)
    if detect_duplicate(page):
        raise RegisterBlocked(step, "duplicate email", is_duplicate=True)


def is_google_redirect(page) -> bool:
    url = (page.url or "").lower()
    if "accounts.google.com" in url:
        return True
    try:
        text = page.locator("body").inner_text(timeout=1000).lower()
        return "sign in with google" in text[:300]
    except Exception:
        return False


def click_primary_button(page, field, labels: list[str]) -> bool:
    """点击 field 所在表单的主按钮(label 匹配 / type=submit / Enter)。

    避免误点 "Continue with Google/Apple" 这类第三方登录按钮。
    """
    label_re = re.compile(rf"^(?:{'|'.join(re.escape(label) for label in labels)})$", re.I)
    try:
        form = field.locator("xpath=ancestor::form[1]").first
        btn = form.get_by_role("button", name=label_re).first
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass
    try:
        form = field.locator("xpath=ancestor::form[1]").first
        btn = form.locator('button[type="submit"], input[type="submit"]').first
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass
    try:
        btn = page.get_by_role("button", name=label_re).last
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass
    try:
        field.press("Enter")
        return True
    except Exception:
        return False


_CF_BODY_MARKERS = (
    "verify you are human", "verifying", "checking your browser",
    "needs to review the security of your connection",
    "performing security verification",       # auth.openai.com full-page interstitial
    "ray id:",                                 # interstitial 末尾固定字串
    "this website uses a security service",   # interstitial 副文案
    "请稍候", "正在验证",
)
# 有这些字串说明 chatgpt SPA 已渲染 → 通过
_CHATGPT_BODY_MARKERS = (
    "log in", "sign up", "continue", "welcome", "openai", "more options",
    "登录", "注册", "继续", "更多选项",
)
_CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'


def _try_click_turnstile(page) -> bool:
    """尝试自动点击 Cloudflare turnstile checkbox(在 iframe 里)。

    成功返回 True。CF 经常识别 playwright click 为 bot,即便点了也可能要二次 puzzle。
    那种 puzzle 就只能换代理。
    """
    try:
        # turnstile iframe 一般 src 含 challenges.cloudflare.com
        # 内部有一个 input[type="checkbox"] 或可点击 div
        cb = page.frame_locator(_CF_IFRAME_SELECTOR).locator('input[type="checkbox"]').first
        try:
            if cb.is_visible(timeout=2000):
                cb.click(timeout=3000, force=True)
                logger.info("[browser] turnstile checkbox 已点击")
                return True
        except Exception:
            pass
        # 另一种:可点击的 label 或 div
        for sel in ('label', 'div[role="button"]', 'div[tabindex]'):
            try:
                el = page.frame_locator(_CF_IFRAME_SELECTOR).locator(sel).first
                if el.is_visible(timeout=1000):
                    el.click(timeout=3000, force=True)
                    logger.info("[browser] turnstile %s 已点击", sel)
                    return True
            except Exception:
                continue
    except Exception as exc:
        logger.debug("[browser] turnstile 自动点击异常: %s", exc)
    return False


def _has_cf_iframe(page) -> bool:
    try:
        return page.locator(_CF_IFRAME_SELECTOR).first.is_visible(timeout=500)
    except Exception:
        return False


def wait_cloudflare(page, max_wait_seconds: int = 120) -> bool:
    """等 Cloudflare turnstile 通过。返回是否通过。

    检测多源:URL 不含 challenge + body 不含 cf 字串 + (body 有 chatgpt 字串 OR body 非空)
    + 如果检测到 turnstile iframe(body 可能为空但 iframe 在),尝试自动点击 checkbox。

    body=0 + iframe 在 = 硬挑战。auto-click 可能成功,可能要二次 puzzle 失败。
    """
    import time as _t

    deadline = _t.time() + max_wait_seconds
    last_log = 0.0
    last_click_attempt = 0.0
    while _t.time() < deadline:
        try:
            url_lower = (page.url or "").lower()
            on_challenge = "challenge" in url_lower
            try:
                body = page.locator("body").inner_text(timeout=2000)
            except Exception:
                body = ""
            body_low = body[:600].lower()
            has_cf = any(m in body_low for m in _CF_BODY_MARKERS)
            has_chatgpt = any(m in body_low for m in _CHATGPT_BODY_MARKERS)
            cf_iframe = _has_cf_iframe(page)

            # success: 没在 challenge URL,body 没 cf 字串,iframe 也消失了,且 SPA 内容已渲染
            if (
                not on_challenge
                and not has_cf
                and not cf_iframe
                and (has_chatgpt or len(body.strip()) > 50)
            ):
                logger.info("[browser] Cloudflare 通过 body_len=%d", len(body))
                return True

            # 看到 turnstile iframe 就尝试点击,5s 间隔避免狂点
            now = _t.time()
            if cf_iframe and now - last_click_attempt > 5:
                _try_click_turnstile(page)
                last_click_attempt = now

            if now - last_log > 5:
                logger.info(
                    "[browser] 等 Cloudflare... on_challenge=%s has_cf=%s has_chatgpt=%s cf_iframe=%s body_len=%d",
                    on_challenge, has_cf, has_chatgpt, cf_iframe, len(body),
                )
                last_log = now
        except Exception as exc:
            logger.debug("[browser] cf 检测异常: %s", exc)
        _t.sleep(2)
    return False


def first_visible_editable(page, selectors: str, timeout: int = 800):
    try:
        loc = page.locator(selectors).first
        if not loc.is_visible(timeout=timeout):
            return None
        if loc.is_editable(timeout=timeout):
            return loc
    except Exception:
        return None
    return None
