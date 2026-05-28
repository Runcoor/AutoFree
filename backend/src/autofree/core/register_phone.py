"""手机号注册 + OAuth 一体化流程 — 完全独立于 register.py / oauth.py 的邮箱路径。

设计原则:
1. **同一个 Playwright session 跑完整流程**:这样 chatgpt.com session cookie 自动带到
   auth.openai.com,避免登录两次。
2. **同一个 SMS 订单收两条 SMS**:5sim/hero-sms 订单都有 20 分钟有效期内可收无限条
   SMS 的特性,我们买一次号:
     - 第 1 条 SMS → chatgpt.com 注册阶段(/contact-verification)
     - 第 2 条 SMS → auth.openai.com OAuth 登录(prompt=login 强制走 /log-in)
   两条都拿到、流程完整跑完 → finish_order 提交扣费(只算 1 次费用)。
   中途任何失败 → cancel/ban_order 退款。
3. **完全双语按钮匹配**:浏览器可能英文,所有 click 走 [中文, English] 候选列表。
4. **不动现有 register.py / oauth.py**:这个模块是平行实现,只复用三个 helper:
     - oauth._pkce / _build_auth_url / _exchange_code(PKCE + token 交换)
   其它 browser / utility 都内部重新实现,免污染上层。

成功 → 返回 bundle dict(同 fetch_personal_bundle 格式)+ phone_verified=True + phone 字段。
失败 → 按阶段抛 RegisterFailed / RegisterBlocked / OAuthFailed,batch.py 已有的兜底逻辑会
自动写 pending 等。
"""

from __future__ import annotations

import logging
import secrets
import time
import urllib.parse
from typing import Any

from playwright.sync_api import Page, sync_playwright

from autofree.core import sms as sms_mod
from autofree.core.browser import (
    email_screenshot_scope,
    get_context_options,
    get_launch_options,
    get_proxy_options,
    make_proxy_session_id,
    safe_screenshot,
    wait_cloudflare,
)
from autofree.core.config import EMAIL_POLL_TIMEOUT, SCREENSHOT_DIR, get_sms_config
from autofree.core.control import is_stop_requested
from autofree.core.errors import BatchStopped, OAuthFailed, RegisterBlocked, RegisterFailed
from autofree.core.identity import random_birthday, random_full_name
from autofree.core.oauth import (
    CODEX_CALLBACK_PORT,
    CODEX_REDIRECT_URI,
    _build_auth_url,
    _exchange_code,
    _pkce,
    _proxy_opts_to_requests,
    assert_account_alive,
    fetch_personal_bundle,
)
from autofree.core.phone_country import PhoneCountry, from_sms_slug, strip_dial_prefix

logger = logging.getLogger(__name__)


# ─── 双语按钮 / 文本常量 ─────────────────────────────────────────────────────

# 手机号注册路径固定密码 — 所有手机号注册号都用这个,便于用户后续手动登录救号 /
# 集中管理。Phase 1 设的、Phase 2 OAuth 用的都是它。
PHONE_REG_PASSWORD = "v7zw8ai29r4ZA"

SIGNUP_BUTTON_TEXTS = ("免费注册", "Sign up for free", "Sign up")
# 密码页底部「Don't have an account? Sign up」链接 — 用于把 login 流程切到 signup
SIGN_UP_LINK_TEXTS = ("Sign up", "注册", "立即注册", "Create account", "创建账号")
# 弹窗 / 注册起始页可能出现的任一文案 — 命中任一即视为就绪
SIGNUP_MODAL_TEXTS = (
    "登录或注册", "Log in or sign up", "Welcome back",
    "Create your account", "创建账户", "创建账号",
    "Welcome to ChatGPT", "欢迎来到 ChatGPT", "欢迎使用 ChatGPT",
    "Sign in to your account", "登录您的账户",
    "继续使用手机登录", "手机登录", "Continue with phone", "Use phone",
    "Continue with email", "继续使用邮箱",
)
PHONE_LOGIN_TEXTS = ("继续使用手机登录", "手机登录", "Continue with phone", "Use phone")
# 中间过渡按钮 — chatgpt.com 新流程:点免费注册后先出现这个按钮,需要先点它才能
# 看到「继续使用手机登录」入口。
INTERMEDIATE_LOGIN_TEXTS = (
    "Log in or sign up to create",
    "登录或注册以创建",
    "Log in or sign up",
    "登录或注册",
)
SUBMIT_BUTTON_TEXTS = ("继续", "Continue", "Next", "下一步")
FINISH_BUTTON_TEXTS = ("完成", "Finish", "Done", "Submit")
RETRY_BUTTON_TEXTS = ("重试", "Retry", "Try again")
ALLOW_BUTTON_TEXTS = (
    "授权", "允许", "同意", "继续",
    "Allow", "Authorize", "Agree", "Accept", "Continue",
)
ACCEPT_COOKIES_TEXTS = (
    "拒绝非必需", "全部接受", "Reject non-essential", "Accept all", "Accept",
)

# Phone 冲突文案(已被注册/绑定)
PHONE_CONFLICT_PATTERNS = (
    "已被绑定", "已绑定", "已注册", "已被注册", "已存在", "已使用",
    "already exists", "already registered", "already in use", "already linked",
    "already have an account",
)

# Phone 格式/无效文案 — 国家选错或号码格式不合法导致前端阻塞提交
PHONE_INVALID_PATTERNS = (
    "Phone number is not valid", "not a valid phone",
    "电话号码无效", "手机号码无效", "号码无效", "格式不正确", "无效的电话",
    "请输入有效的", "Invalid phone number",
)

# 选择器
PHONE_INPUT_SELECTOR = 'input[name="phoneNumberInput"], input[type="tel"]'
PASSWORD_INPUT_SELECTOR = 'input[type="password"]'
CODE_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[name="code"]',
    'input[inputmode="numeric"]:not([name="phoneNumberInput"])',
    'input[type="text"][maxlength="6"]',
    'input[type="tel"][maxlength="1"]',  # 6-cell UI
)

# SMS 等待参数
SMS_WAIT_SECONDS = 120
MAX_SMS_BUY_ATTEMPTS = 10  # 整个流程最多换号几次(NO_NUMBERS / 死号 / 二手号)


# ─── 通用工具(为手机号路径单独实现,免污染 oauth.py)──────────────────────────

def _sleep(seconds: float) -> None:
    """协作式 sleep — 中途检查 stop 信号,响应延迟 ≤ 0.5s。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if is_stop_requested():
            raise BatchStopped("phone reg sleep 中收到 stop 信号")
        time.sleep(min(0.5, max(0.0, deadline - time.time())))


def _click_button_by_text(page: Page, candidates: tuple[str, ...], *, timeout_ms: int = 10000) -> bool:
    """按文字匹配点击(button / [role=button] / a),全鼠标事件链触发 React。

    返回是否点到。任何 candidate 命中即返回 True。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            clicked = page.evaluate(
                """(texts) => {
                    const nodes = document.querySelectorAll('button, [role="button"], a');
                    for (const b of nodes) {
                        const t = (b.innerText || b.textContent || '').trim();
                        if (!t) continue;
                        if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                        const rect = b.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        if (texts.some(tx => t.includes(tx))) {
                            ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                                b.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                            });
                            return t;
                        }
                    }
                    return null;
                }""",
                list(candidates),
            )
            if clicked:
                logger.info("[phone-reg] 点击按钮: %r (候选 %s)", clicked, candidates)
                return True
        except Exception as exc:
            logger.debug("[phone-reg] click 评估失败: %s", exc)
        time.sleep(0.5)
    return False


def _wait_text_on_page(page: Page, candidates: tuple[str, ...], *, timeout_ms: int = 30000) -> bool:
    """等待 body 文本出现某些字符串。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            found = page.evaluate(
                """(texts) => {
                    const body = (document.body && document.body.innerText) || '';
                    return texts.some(t => body.includes(t));
                }""",
                list(candidates),
            )
            if found:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wait_button_by_text(page: Page, candidates: tuple[str, ...], *, timeout_ms: int = 30000) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            found = page.evaluate(
                """(texts) => {
                    const nodes = document.querySelectorAll('button, [role="button"], a');
                    for (const b of nodes) {
                        const t = (b.innerText || b.textContent || '').trim();
                        if (texts.some(tx => t.includes(tx))) return true;
                    }
                    return false;
                }""",
                list(candidates),
            )
            if found:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wait_continue_enabled(page: Page, max_wait_s: float = 5.0) -> bool:
    """等页面上 type=submit 或文本含 Continue/继续 的按钮变成 enabled。

    OpenAI 密码页填密码后,前端异步校验复杂度(8位+大小写+数字),通过前
    Continue 按钮 disabled。此时点击等于没点。本函数轮询等启用,超时返 False
    但调用方仍可尝试点(也许文本不匹配但能强点)。
    """
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            enabled = page.evaluate(
                """(texts) => {
                    const all = document.querySelectorAll('button[type="submit"], button');
                    for (const b of all) {
                        if (b.disabled) continue;
                        const t = (b.innerText || '').trim();
                        if (!t) continue;
                        if (texts.some(tx => t === tx || t.includes(tx))) return true;
                    }
                    return false;
                }""",
                ["Continue", "继续", "Next", "下一步"],
            )
            if enabled:
                return True
        except Exception:
            pass
        _sleep(0.3)
    return False


def _click_submit_button(page: Page) -> bool:
    """点页面上的「继续 / Continue / Next」提交按钮(优先 type=submit)。"""
    try:
        clicked = page.evaluate(
            """(texts) => {
                // 优先精确文本匹配 + type=submit
                const submits = document.querySelectorAll('button[type="submit"]');
                for (const b of submits) {
                    const t = (b.innerText || '').trim();
                    if (b.disabled) continue;
                    if (texts.some(tx => t === tx || t.includes(tx))) {
                        ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                            b.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                        });
                        return t;
                    }
                }
                // 兜底:任何 button 含候选文字
                for (const b of document.querySelectorAll('button')) {
                    const t = (b.innerText || '').trim();
                    if (b.disabled) continue;
                    if (texts.some(tx => t === tx || t.includes(tx))) {
                        ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                            b.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                        });
                        return t;
                    }
                }
                return null;
            }""",
            list(SUBMIT_BUTTON_TEXTS + FINISH_BUTTON_TEXTS),
        )
        if clicked:
            logger.info("[phone-reg] submit 点击: %r", clicked)
            return True
    except Exception as exc:
        logger.debug("[phone-reg] submit 评估失败: %s", exc)
    return False


def _click_submit_button_real(page: Page, timeout_ms: int = 8000) -> bool:
    """Playwright 真点击 — 产生 event.isTrusted=true 的原生鼠标事件。

    /add-email 这种反爬严的页面只接受 isTrusted=true 的提交,dispatchEvent 合成事件
    会被 OpenAI 后端 silent drop(UI 跳到 'Check your inbox' 但邮件永远不发)。
    跟 register.py 的 click_primary_button 行为一致。
    失败回退到 _click_submit_button(dispatchEvent),保持兼容老路径。
    """
    candidates = list(SUBMIT_BUTTON_TEXTS) + list(FINISH_BUTTON_TEXTS)
    for text in candidates:
        try:
            btn = page.locator(f'button[type="submit"]:has-text("{text}")').first
            if btn.is_visible(timeout=1500) and not btn.is_disabled(timeout=500):
                btn.click(timeout=timeout_ms)
                logger.info("[phone-reg] submit 真点击: %r", text)
                return True
        except Exception:
            continue
    for text in candidates:
        try:
            btn = page.locator(f'button:has-text("{text}")').first
            if btn.is_visible(timeout=1500) and not btn.is_disabled(timeout=500):
                btn.click(timeout=timeout_ms)
                logger.info("[phone-reg] submit 真点击(非 submit type): %r", text)
                return True
        except Exception:
            continue
    logger.warning("[phone-reg] 真点击没找到按钮 — 回退 dispatchEvent")
    return _click_submit_button(page)


def _dismiss_cookie_banner(page: Page) -> None:
    """优先点「拒绝非必需」,失败回退「全部接受」— 不阻塞主流程。"""
    if _click_button_by_text(page, ACCEPT_COOKIES_TEXTS, timeout_ms=1500):
        _sleep(0.8)


def _detect_phone_conflict(page: Page) -> str | None:
    """检测页面文本里是否含「号码已被使用」类提示。返回命中行,无则 None。"""
    try:
        body = page.locator("body").inner_text(timeout=1500) or ""
    except Exception:
        return ""
    low = body.lower()
    for pat in PHONE_CONFLICT_PATTERNS:
        if pat in body or pat.lower() in low:
            for line in body.split("\n"):
                line_strip = line.strip()
                if pat in line_strip or pat.lower() in line_strip.lower():
                    return line_strip[:200]
            return pat
    return None


def _detect_phone_invalid(page: Page) -> str | None:
    """检测页面是否有「号码无效 / Phone number is not valid」类前端校验阻塞文案。

    出现这个文案通常意味着:国家代码选错 / 号码长度不对 / OpenAI 拒收该号段。
    SMS 不会被触发,需要立即换号(或换国家)。"""
    try:
        body = page.locator("body").inner_text(timeout=1500) or ""
    except Exception:
        return None
    low = body.lower()
    for pat in PHONE_INVALID_PATTERNS:
        if pat in body or pat.lower() in low:
            for line in body.split("\n"):
                line_strip = line.strip()
                if pat in line_strip or pat.lower() in line_strip.lower():
                    return line_strip[:200]
            return pat
    return None


def _classify_password_page(page: Page) -> str:
    """密码页文本分类:'create' / 'existing' / 'unknown'。

    同一个 URL(例如 /create-account/password 或 /create-account/x)下页面
    可能是两种状态:
      - "Create your password" / "Create a password" / "Set a password" / 中文 — 新号
        创建密码,需要填 password 继续
      - "Enter your password" / "Forgot password?" / 中文 — 已注册号登录页,必须
        ban 该号换新号(因为我们注册新号时不该撞到此页)

    返回 'unknown' 表示不是密码相关页(让上层走原本的分支)。"""
    try:
        body = page.locator("body").inner_text(timeout=1500) or ""
    except Exception:
        return "unknown"
    body_low = body.lower()
    # ★ 最强信号:内嵌「号码已注册」警告(常见于 Create a password 页底部红字)—
    # 必须先于 create 判,否则会误以为是新号填密码 → 点 Continue 死循环
    for pat in PHONE_CONFLICT_PATTERNS:
        if pat.lower() in body_low or pat in body:
            return "existing"
    # 已存在账号特征(Forgot password 链接 / Enter your password 标题)
    if "forgot password" in body_low or "忘记密码" in body or "找回密码" in body:
        return "existing"
    if "enter your password" in body_low or "输入你的密码" in body or "输入密码" in body:
        return "existing"
    # 新号创建密码特征
    if ("create your password" in body_low or "create a password" in body_low
            or "set a password" in body_low or "set your password" in body_low):
        return "create"
    if "创建密码" in body or "设置密码" in body or "请创建密码" in body:
        return "create"
    return "unknown"


def _detect_oauth_error(page: Page) -> str | None:
    """auth.openai.com 出错页(no_valid_organizations / something went wrong)。"""
    try:
        body = (page.locator("body").inner_text(timeout=1000) or "").lower()
    except Exception:
        return None
    markers = (
        "no_valid_organizations",
        "oops, an error occurred",
        "something went wrong",
        "an error occurred during authentication",
        "出错了",
        "糟糕",
    )
    for m in markers:
        if m in body:
            return m
    return None


# ─── 国家选择器(chatgpt.com 注册弹窗 + auth.openai.com 登录页两种 UI)─────────

def _select_country(page: Page, country: PhoneCountry) -> bool:
    """选国家 — 支持原生 <select> 和 React Aria Select(隐藏 select + 按钮)两种 UI。

    返回是否选成功。失败不抛错(让上层用全号码兜底)。"""
    iso = country.iso_code
    dial = country.dial_code
    name = country.cn_name

    # 找触发器按钮 — 多种候选,从严到松,绝不扫 listbox 内的选项
    def _find_trigger_text() -> str | None:
        return page.evaluate(
            """() => {
                // 候选触发器:必须可见、不在 listbox role 容器内
                const isHidden = (el) => {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return true;
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0')
                        return true;
                    return false;
                };
                const inListbox = (el) => {
                    let p = el.parentElement;
                    while (p && p !== document.body) {
                        if (p.getAttribute && p.getAttribute('role') === 'listbox') return true;
                        p = p.parentElement;
                    }
                    return false;
                };
                const candidates = [];
                // 候选 1: aria-haspopup=listbox
                for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
                    if (!isHidden(b) && !inListbox(b)) candidates.push(b);
                }
                // 候选 2: role=combobox
                for (const b of document.querySelectorAll('[role="combobox"]')) {
                    if (!isHidden(b) && !inListbox(b)) candidates.push(b);
                }
                // 候选 3: 兜底 — 任何可见 button 文本末尾匹配 "(+XX)" 模式(国家选择器特征)
                for (const b of document.querySelectorAll('button')) {
                    if (!isHidden(b) || inListbox(b)) continue;
                    const t = (b.innerText || '').trim();
                    // 严格匹配: 末尾或括号内含 +XX (1-4 位数字),且整体文本长度 < 60 (避免大段文字)
                    if (t.length < 60 && (/\\(\\+?\\d{1,4}\\)/.test(t) || /\\+\\d{1,4}\\s*$/.test(t))) {
                        candidates.push(b);
                    }
                }
                if (candidates.length === 0) return null;
                return (candidates[0].innerText || '').trim();
            }"""
        )

    trigger_text = _find_trigger_text()
    logger.info("[phone-reg] 触发器文本: %r (目标 iso=%s dial=+%s)", trigger_text, iso, dial)

    # 已经显示正确国家? — 看 trigger 文本或 select.options[selectedIndex] 是否含 +{dial}
    if trigger_text:
        import re as _re
        if _re.search(rf"(^|[^0-9])\+{dial}([^0-9]|$)", trigger_text) or f"({dial})" in trigger_text:
            logger.info("[phone-reg] 国家已是 %s", trigger_text)
            return True
    # 退路:原生 select 的当前选中项
    sel_already = page.evaluate(
        """(args) => {
            const { iso, dial } = args;
            const sel = document.querySelector('select');
            if (!sel) return null;
            const opt = sel.options[sel.selectedIndex];
            if (!opt) return null;
            if (opt.value === iso) return opt.text;
            const re = new RegExp(`(^|[^0-9])\\\\+${dial}([^0-9]|$)`);
            if (re.test(opt.text) || opt.text.includes(`(${dial})`)) return opt.text;
            return null;
        }""",
        {"iso": iso, "dial": dial},
    )
    if sel_already:
        logger.info("[phone-reg] 国家已是 select:%s", sel_already)
        return True

    # 检测 UI 类型 — 注意顺序:Radix combobox 优先,因为它没有底层 <select>,
    # 也没有 aria-haspopup=listbox(用 role=combobox)
    ui_type = page.evaluate(
        """() => {
            // Radix UI Select: role=combobox + aria-controls=radix-XXX
            const radixBtn = Array.from(document.querySelectorAll('[role="combobox"]')).find(
                b => /\\+\\d/.test(b.innerText || '') || /country/i.test(b.getAttribute('aria-label') || '')
            );
            if (radixBtn) return 'radix-combobox';
            // 旧版 React Aria: button + aria-haspopup=listbox
            const hasBtn = Array.from(document.querySelectorAll('button')).some(
                b => b.getAttribute('aria-haspopup') === 'listbox' && /\\+\\d/.test(b.innerText || '')
            );
            if (hasBtn) return 'react-aria';
            const hasSelect = !!document.querySelector('select');
            if (hasSelect) return 'native';
            return 'unknown';
        }"""
    )
    logger.info("[phone-reg] 国家选择器 UI=%s 目标 iso=%s dial=%s", ui_type, iso, dial)

    # 英文国家名(用于 listbox 文本匹配 / 键盘搜索) — PhoneCountry 用 en_aliases tuple
    en_name = (country.en_aliases[0] if country.en_aliases else country.cn_name) or ""

    # ── Radix UI Select(chatgpt.com 新版常见,role=combobox + portal listbox)──
    if ui_type == "radix-combobox":
        try:
            # 1. 点开下拉
            btn_box = page.evaluate(
                """() => {
                    const b = Array.from(document.querySelectorAll('[role="combobox"]')).find(
                        x => /\\+\\d/.test(x.innerText || '') || /country/i.test(x.getAttribute('aria-label') || '')
                    );
                    if (!b) return null;
                    const r = b.getBoundingClientRect();
                    return { x: r.x + r.width/2, y: r.y + r.height/2 };
                }"""
            )
            if not btn_box:
                logger.warning("[phone-reg] Radix combobox 按钮坐标取不到")
            else:
                page.mouse.click(btn_box["x"], btn_box["y"])
                _sleep(1.5)
                # 2. 等 listbox 出现 — Radix 通常 portal 到 body
                listbox_ready = page.evaluate(
                    """() => !!document.querySelector('[role="listbox"]')"""
                )
                if not listbox_ready:
                    logger.warning("[phone-reg] Radix 下拉 1.5s 内没出现 listbox")
                else:
                    # 3. 在 listbox 里找目标 option — Radix option 用 role=option
                    # 文本通常类似 "Brazil +55" / "Brazil (+55)" / "Brazil"
                    # 尝试多种文本匹配
                    target = page.evaluate(
                        """(args) => {
                            const { iso, dial, name } = args;
                            const opts = document.querySelectorAll('[role="option"]');
                            for (const o of opts) {
                                const t = (o.innerText || '').trim();
                                // 优先精确匹配国家名 + dial 组合
                                if (name && t.includes(name) && t.includes(dial)) return { idx: o.dataset.index || -1, text: t, found: true };
                            }
                            for (const o of opts) {
                                const t = (o.innerText || '').trim();
                                if (t.includes(`+${dial}`) || t.includes(`(${dial})`) || t.includes(`(+${dial})`)) {
                                    // 进一步确认是目标国家:看 data-value / data-key 是否匹配 iso
                                    const dv = o.getAttribute('data-value') || o.getAttribute('data-key') || '';
                                    if (!dv || dv.toUpperCase() === iso.toUpperCase() || t.toLowerCase().includes(name.toLowerCase())) {
                                        return { idx: -1, text: t, found: true };
                                    }
                                }
                            }
                            return { found: false, total: opts.length };
                        }""",
                        {"iso": iso, "dial": dial, "name": en_name.strip()},
                    )
                    logger.info("[phone-reg] Radix listbox 目标搜索: %s", target)
                    if target.get("found"):
                        # 4. 滚动 + 点击。Radix listbox 通常 virtualized,需要把目标 scroll 进视区
                        # 先尝试 keyboard: 输入国家名首字母 / 全名(combobox 通常支持类型搜索)
                        # 简化做法:遍历点击直到找到可见的目标
                        clicked = page.evaluate(
                            """(args) => {
                                const { iso, dial, name } = args;
                                const opts = document.querySelectorAll('[role="option"]');
                                for (const o of opts) {
                                    const t = (o.innerText || '').trim();
                                    const dv = (o.getAttribute('data-value') || o.getAttribute('data-key') || '').toUpperCase();
                                    const isTarget = (dv === iso.toUpperCase()) ||
                                        (name && t.includes(name) && (t.includes(`+${dial}`) || t.includes(`(${dial})`))) ||
                                        (t === `${name} +${dial}` || t === `${name} (+${dial})`);
                                    if (isTarget) {
                                        o.scrollIntoView({ block: 'center' });
                                        o.click();
                                        return t;
                                    }
                                }
                                return null;
                            }""",
                            {"iso": iso, "dial": dial, "name": en_name.strip()},
                        )
                        if clicked:
                            _sleep(0.8)
                            logger.info("[phone-reg] Radix combobox 选择成功: %s", clicked)
                            return True
                        logger.warning("[phone-reg] Radix listbox 找到目标但点击失败")
                    else:
                        # 没找到:可能列表很长 + virtualized + 不在 DOM 里。试键盘输入搜索
                        logger.info("[phone-reg] Radix listbox 直接遍历未中,试键盘输入国家名搜索 (total=%s)",
                                    target.get("total"))
                        try:
                            type_name = en_name.strip()
                            if type_name:
                                page.keyboard.type(type_name, delay=80)
                                _sleep(1.0)
                                clicked = page.evaluate(
                                    """(args) => {
                                        const { iso, dial, name } = args;
                                        // 输入后 listbox 通常只剩匹配项,取第一个 role=option 点
                                        const opts = document.querySelectorAll('[role="option"]');
                                        for (const o of opts) {
                                            const t = (o.innerText || '').trim();
                                            if (name && t.toLowerCase().startsWith(name.toLowerCase())) {
                                                o.scrollIntoView({ block: 'center' });
                                                o.click();
                                                return t;
                                            }
                                        }
                                        // 都没匹配,点第一个
                                        if (opts.length > 0) {
                                            opts[0].scrollIntoView({ block: 'center' });
                                            opts[0].click();
                                            return (opts[0].innerText || '').trim();
                                        }
                                        return null;
                                    }""",
                                    {"iso": iso, "dial": dial, "name": type_name},
                                )
                                if clicked:
                                    _sleep(0.8)
                                    logger.info("[phone-reg] Radix combobox 键盘搜索+点击成功: %s", clicked)
                                    return True
                        except Exception as exc:
                            logger.warning("[phone-reg] Radix 键盘搜索异常: %s", exc)
                    # 关闭下拉避免污染后续步骤
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("[phone-reg] Radix combobox 选择异常: %s", exc)

    # ── React Aria(auth.openai.com 登录页常见)──
    if ui_type == "react-aria":
        # 方法 A: 操作底层隐藏 <select>,nativeSetter + change 事件
        ok = page.evaluate(
            """(iso) => {
                const sel = document.querySelector('select');
                if (!sel) return false;
                const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
                setter.call(sel, iso);
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                for (const b of document.querySelectorAll('button')) {
                    if (b.getAttribute('aria-haspopup') === 'listbox') return (b.innerText || '').trim();
                }
                return 'changed';
            }""",
            iso,
        )
        if ok and (f"+{dial}" in str(ok) or str(ok) == "changed"):
            logger.info("[phone-reg] React Aria 国家选择(隐藏 select)成功: %s", ok)
            _sleep(0.5)
            return True
        logger.info("[phone-reg] React Aria 隐藏 select 法失败,尝试点击下拉")

        # 方法 B: 点开下拉 → 滚到目标 → 真鼠标点击 option[data-key=ISO]
        btn_box = page.evaluate(
            """() => {
                for (const b of document.querySelectorAll('button')) {
                    if (b.getAttribute('aria-haspopup') === 'listbox' && /\\+\\d/.test(b.innerText || '')) {
                        const r = b.getBoundingClientRect();
                        return { x: r.x + r.width/2, y: r.y + r.height/2 };
                    }
                }
                return null;
            }"""
        )
        if btn_box:
            try:
                page.mouse.click(btn_box["x"], btn_box["y"])
                _sleep(1.5)
                target_index = page.evaluate(
                    """(iso, name) => {
                        const sel = document.querySelector('select');
                        if (!sel) return -1;
                        for (let i = 0; i < sel.options.length; i++) {
                            if (sel.options[i].value === iso) return i;
                            if (name && sel.options[i].text.includes(name)) return i;
                        }
                        return -1;
                    }""",
                    iso, name,
                )
                if target_index >= 0:
                    page.evaluate(
                        """(idx) => {
                            const listbox = document.querySelector('[role="listbox"]');
                            if (!listbox) return;
                            let scroller = listbox;
                            while (scroller && scroller !== document.body) {
                                const st = getComputedStyle(scroller);
                                if (st.overflow === 'auto' || st.overflow === 'scroll' ||
                                    st.overflowY === 'auto' || st.overflowY === 'scroll') break;
                                scroller = scroller.parentElement;
                            }
                            if (scroller) scroller.scrollTop = idx * 40;
                        }""",
                        target_index,
                    )
                    _sleep(0.6)
                    opt_box = page.evaluate(
                        """(iso) => {
                            const el = document.querySelector(`[data-key="${iso}"]`);
                            if (!el || el.offsetParent === null) return null;
                            const r = el.getBoundingClientRect();
                            return { x: r.x + r.width/2, y: r.y + r.height/2 };
                        }""",
                        iso,
                    )
                    if opt_box:
                        page.mouse.click(opt_box["x"], opt_box["y"])
                        _sleep(0.8)
                        logger.info("[phone-reg] React Aria 滚动点击成功 iso=%s", iso)
                        return True
                page.keyboard.press("Escape")
            except Exception as exc:
                logger.warning("[phone-reg] React Aria 滚动点击异常: %s", exc)

    # ── 原生 <select>(chatgpt.com 注册弹窗常见)──
    # 关键: 必须用 nativeSetter,直接 sel.value=X 不会触发 React 受控组件的 onChange
    if ui_type in ("native", "unknown"):
        ok = page.evaluate(
            """(args) => {
                const { iso, dial, name } = args;
                const sel = document.querySelector('select');
                if (!sel) return null;
                let target = null;
                for (const opt of sel.options) {
                    if (opt.value === iso) { target = opt; break; }
                    if (opt.text.includes(`(${dial})`) || opt.text.includes(`+${dial}`)) { target = opt; break; }
                    if (name && opt.text.includes(name)) { target = opt; break; }
                }
                if (!target) return null;
                // 用 nativeSetter 触发 React 受控组件更新
                const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
                setter.call(sel, target.value);
                sel.dispatchEvent(new Event('input', { bubbles: true }));
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return target.text;
            }""",
            {"iso": iso, "dial": dial, "name": name},
        )
        if ok:
            logger.info("[phone-reg] 原生 select 选择: %s,校验中...", ok)
            _sleep(0.8)
            # 校验:用同样宽松的 trigger 检测重读
            verify_text = _find_trigger_text()
            actual_dial = ""
            if verify_text:
                import re as _re
                # 兼容 +55 / +(55) / (+55) 三种写法
                m = _re.search(r"\+\(?(\d+)", verify_text)
                if m:
                    actual_dial = m.group(1)
            if not actual_dial:
                # 兜底:select
                actual_dial = page.evaluate(
                    """() => {
                        const sel = document.querySelector('select');
                        if (sel && sel.options[sel.selectedIndex]) {
                            const m = sel.options[sel.selectedIndex].text.match(/\\+\\(?(\\d+)/);
                            if (m) return m[1];
                        }
                        return '';
                    }"""
                ) or ""
            if str(actual_dial) == dial:
                logger.info("[phone-reg] 国家校验通过 dial=+%s", actual_dial)
                return True
            logger.warning(
                "[phone-reg] 国家校验失败:期望 +%s 实际 +%s trigger=%r — dump 页面 button",
                dial, actual_dial or "?", verify_text,
            )
            # 失败时 dump 全部 visible button 文本,排查 chatgpt.com 改版
            try:
                btns = page.evaluate(
                    """() => {
                        const out = [];
                        for (const b of document.querySelectorAll('button, [role="combobox"], [role="button"]')) {
                            const r = b.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            const t = (b.innerText || '').trim();
                            if (!t || t.length > 100) continue;
                            const attrs = {};
                            for (const a of b.attributes) attrs[a.name] = a.value;
                            out.push({ text: t, attrs });
                            if (out.length >= 30) break;
                        }
                        return out;
                    }"""
                )
                logger.error("[phone-reg] 国家失败诊断 visible_buttons=%s", btns)
            except Exception:
                pass
            return False

    logger.warning("[phone-reg] 国家选择全部方法失败,流程将用完整号码兜底")
    return False


# ─── SMS 订单管理 ───────────────────────────────────────────────────────────

def _buy_phone_order(sms_provider, sms_cfg: dict, attempt_idx: int) -> Any:
    """买 SMS 号 — 任何失败/库存空抛 SmsBuyFailed,上层重试。

    保护层:provider 实现里 HTTP 404 / 网络异常等通用错误抛的是 SmsError(父类),
    这里捕获后转成 SmsBuyFailed,统一让 phase1 retry 循环吃下,不要冒泡到 batch
    被当作「未预期异常 → oauth_failed」(NO_NUMBERS 是可重试的临时问题)。
    """
    country = sms_cfg.get("country") or sms_provider.DEFAULT_COUNTRY
    operator = sms_cfg.get("operator") or sms_provider.DEFAULT_OPERATOR
    service = sms_cfg.get("service") or sms_provider.DEFAULT_SERVICE
    # max_price 来自设置 — None 或 0 表示不限价
    max_price_raw = sms_cfg.get("max_price")
    try:
        max_price = float(max_price_raw) if max_price_raw not in (None, "", 0) else None
    except (TypeError, ValueError):
        max_price = None
    logger.info(
        "[phone-reg] 买号 attempt=%d provider=%s country=%s operator=%s service=%s max_price=%s",
        attempt_idx, sms_provider.PROVIDER_NAME, country, operator, service,
        f"${max_price}" if max_price else "不限",
    )
    try:
        return sms_provider.buy_activation(
            country=country, operator=operator, product=service, max_price=max_price,
        )
    except (sms_mod.SmsBuyFailed, sms_mod.SmsConfigMissing):
        raise
    except sms_mod.SmsError as exc:
        # provider 通用错误(HTTP 4xx/5xx、网络异常、未识别响应等)— 转可重试
        msg = str(exc).lower()
        if any(k in msg for k in ("no_numbers", "no numbers", "numbers not found",
                                  "out of stock", "no stock")):
            raise sms_mod.SmsBuyFailed(
                f"库存空(provider 当下没号 / max_price 太低过滤光了): {exc}"
            ) from exc
        raise sms_mod.SmsBuyFailed(f"买号失败(可重试): {exc}") from exc


def _safe_refund(sms_provider, order, kind: str = "cancel") -> None:
    """退款 — kind=cancel(SMS 未到时全退)/ ban(死号全退 + 不再分同号)。失败只 log。"""
    if not order:
        return
    try:
        if kind == "ban":
            sms_provider.ban_order(order.id)
        else:
            sms_provider.cancel_order(order.id)
        logger.info("[phone-reg] %s order=%d 完成", kind, order.id)
    except Exception as exc:
        logger.warning("[phone-reg] %s order=%s 失败(已扣费可能): %s", kind, getattr(order, "id", "?"), exc)


# ─── 表单填写 helpers ───────────────────────────────────────────────────────

def _back_to_phone_input(page: Page, country: PhoneCountry) -> bool:
    """确保 page 处于手机号输入态。已在 → True 直接返回;
    不在(通常是 attempt N>1 时停在密码页/SMS code 页) → 尝试点
    「Edit」链接回手机输入页;失败则 page.goto chatgpt.com 重走 modal。

    返回是否成功恢复。"""
    # 已经在输入页?
    try:
        if page.locator(PHONE_INPUT_SELECTOR).first.is_visible(timeout=1500):
            return True
    except Exception:
        pass

    # 试点「Edit」链接 — phone_05 截图密码页里 Phone number 旁边的 Edit
    logger.info("[phone-reg] 当前不在手机输入页,尝试点 Edit 回去")
    edit_clicked = _click_button_by_text(
        page, ("Edit", "编辑", "修改"), timeout_ms=2500,
    )
    if edit_clicked:
        _sleep(1.5)
        try:
            if page.locator(PHONE_INPUT_SELECTOR).first.is_visible(timeout=4000):
                logger.info("[phone-reg] Edit 命中,已回手机输入页")
                return True
        except Exception:
            pass

    # 重型兜底:重新加载 chatgpt.com 走 modal → phone login
    logger.info("[phone-reg] Edit 不可用,重新 goto chatgpt.com 走 modal")
    try:
        page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60000)
        _sleep(3)
        wait_cloudflare(page, max_wait_seconds=60)
        _sleep(2)
        # 新版可能直接落登录页,否则点免费注册
        if not page.locator(PHONE_INPUT_SELECTOR).first.is_visible(timeout=1500):
            # 优先看到手机登录按钮直接点
            if _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=3000):
                _sleep(2)
            else:
                # 老版 landing — 点免费注册再点手机登录(中间可能有过渡按钮)
                if _click_button_by_text(page, SIGNUP_BUTTON_TEXTS, timeout_ms=5000):
                    _sleep(3)
                    # 可能出现「Log in or sign up to create」中间按钮
                    if not _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=2500):
                        if _click_button_by_text(page, INTERMEDIATE_LOGIN_TEXTS, timeout_ms=2500):
                            _sleep(2)
                            _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=5000)
                    _sleep(2)
        page.locator(PHONE_INPUT_SELECTOR).first.wait_for(state="visible", timeout=10000)
        # 重新选国家(state 重置了)
        try:
            _select_country(page, country)
            _sleep(1)
        except Exception:
            pass
        logger.info("[phone-reg] goto 兜底成功,回到手机输入页")
        return True
    except Exception as exc:
        logger.warning("[phone-reg] goto 兜底也失败: %s", exc)
        return False


def _fill_phone_input(page: Page, local_number: str, full_number: str, country: PhoneCountry) -> None:
    """填手机号:先看页面国家显示是否对,对就填本地号,不对就填完整号(不带 +)。

    JS focus + keyboard.type — 绕开 React Aria 浮动 label 拦截 click。
    """
    inp = page.locator(PHONE_INPUT_SELECTOR).first
    inp.wait_for(state="visible", timeout=15000)
    # JS focus(避免 floating label overlay 拦截 click)
    page.evaluate(
        """() => {
            const inp = document.querySelector('input[name="phoneNumberInput"], input[type="tel"]');
            if (inp) inp.focus();
        }"""
    )
    _sleep(0.15)

    # 找触发器读 current_dial — 宽松匹配:aria-haspopup / role=combobox / 含 (+XX)
    # 文本的可见 button,且不在 listbox 容器内(避免读到隐藏选项)
    current_dial = page.evaluate(
        """() => {
            const isHidden = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) return true;
                const cs = getComputedStyle(el);
                return cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0';
            };
            const inListbox = (el) => {
                let p = el.parentElement;
                while (p && p !== document.body) {
                    if (p.getAttribute && p.getAttribute('role') === 'listbox') return true;
                    p = p.parentElement;
                }
                return false;
            };
            const candidates = [];
            for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
                if (!isHidden(b) && !inListbox(b)) candidates.push(b);
            }
            for (const b of document.querySelectorAll('[role="combobox"]')) {
                if (!isHidden(b) && !inListbox(b)) candidates.push(b);
            }
            for (const b of document.querySelectorAll('button')) {
                if (isHidden(b) || inListbox(b)) continue;
                const t = (b.innerText || '').trim();
                if (t.length < 60 && (/\\(\\+?\\d{1,4}\\)/.test(t) || /\\+\\d{1,4}\\s*$/.test(t))) {
                    candidates.push(b);
                }
            }
            for (const b of candidates) {
                // 兼容 +55 / +(55) / (+55) 三种写法
                const m = (b.innerText || '').match(/\\+\\(?(\\d+)/);
                if (m) return m[1];
            }
            const sel = document.querySelector('select');
            if (sel && sel.options[sel.selectedIndex]) {
                const m = sel.options[sel.selectedIndex].text.match(/\\+\\(?(\\d+)/);
                if (m) return m[1];
            }
            return '';
        }"""
    )

    if current_dial and str(current_dial) == country.dial_code:
        value = local_number
        logger.info("[phone-reg] 国家显示 +%s 已对,填本地号 %s", current_dial, value)
    else:
        value = full_number.lstrip("+")
        logger.info("[phone-reg] 国家显示 +%s 与目标 +%s 不符,填完整号 %s",
                    current_dial or "?", country.dial_code, value)

    # 清空 — Ctrl+A + Delete(不用 inp.fill 避免 actionability check 卡住)
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    except Exception:
        pass
    _sleep(0.2)
    page.keyboard.type(value, delay=50)
    _sleep(0.5)


def _fill_password_input(page: Page, password: str) -> None:
    """填密码 — JS focus + 真实键盘 type,React Aria 浮动 label 兼容。

    chatgpt.com create-password 页用 React Aria 浮动 label(`_typeableLabel`),
    overlay 拦截 Playwright .click() → 30s actionability 超时把整个 phase1 拖死。
    所以不用 click,直接 JS focus 后用 keyboard.type(NumberField 风格的 input
    要求逐字符输入,nativeSetter 会被 React 受控组件覆盖)。
    """
    # 等密码 input 渲染 — 只看 DOM,不用 click 触发 actionability
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ready = page.evaluate(
            """() => !!document.querySelector('input[type="password"]')"""
        )
        if ready:
            break
        _sleep(0.3)

    # JS focus(绕开 floating label overlay)
    focused = page.evaluate(
        """() => {
            const inp = document.querySelector('input[type="password"]');
            if (!inp) return false;
            inp.focus();
            return document.activeElement === inp;
        }"""
    )
    if not focused:
        logger.warning("[phone-reg] 密码输入框 focus 失败,尝试 force click 兜底")
        try:
            page.locator(PASSWORD_INPUT_SELECTOR).first.click(force=True, timeout=3000)
        except Exception as exc:
            logger.warning("[phone-reg] force click 也失败: %s — type 可能填到错位置", exc)
    _sleep(0.2)

    # 清空(Ctrl+A + Delete,比 Backspace 稳)
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    except Exception:
        pass

    page.keyboard.type(password, delay=30)
    _sleep(0.4)

    # 校验:读 input.value 长度,空就再试一次 force click + type
    actual_len = page.evaluate(
        """() => (document.querySelector('input[type="password"]') || {}).value?.length || 0"""
    )
    if actual_len != len(password):
        logger.warning(
            "[phone-reg] 密码填写校验失败 (设 %d 字符 / 实际 %d) — 重试",
            len(password), actual_len,
        )
        try:
            page.locator(PASSWORD_INPUT_SELECTOR).first.click(force=True, timeout=5000)
            _sleep(0.2)
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.type(password, delay=60)
            _sleep(0.4)
            actual_len = page.evaluate(
                """() => (document.querySelector('input[type="password"]') || {}).value?.length || 0"""
            )
            logger.info("[phone-reg] 密码重试后长度=%d", actual_len)
        except Exception as exc:
            logger.warning("[phone-reg] 密码重试异常: %s", exc)


def _fill_sms_code_smart(page: Page, code: str) -> bool:
    """智能 SMS code 填:6 格 UI / 单 input UI 都支持。返回是否成功。"""
    code = (code or "").strip()
    if not code:
        return False

    # 6 格模式
    for sel in CODE_INPUT_SELECTORS:
        try:
            cnt = page.locator(sel).count()
            if cnt >= 6:
                cells = page.locator(sel)
                visible_cells = []
                for i in range(min(cnt, 6)):
                    cell = cells.nth(i)
                    try:
                        if cell.is_visible(timeout=300):
                            visible_cells.append(cell)
                    except Exception:
                        pass
                if len(visible_cells) >= len(code):
                    logger.info("[phone-reg] SMS code 6-cell 模式 cells=%d", len(visible_cells))
                    try:
                        # JS 模式:focus 第一格 + 顺序 type,Radix/React-Aria 的 6 格
                        # OTP input 通常自动跳到下一格
                        page.evaluate(
                            """(s) => {
                                const cells = document.querySelectorAll(s);
                                for (const c of cells) {
                                    const r = c.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) { c.focus(); return; }
                                }
                            }""",
                            sel,
                        )
                        time.sleep(0.15)
                        for ch in code:
                            page.keyboard.type(ch, delay=80)
                            time.sleep(0.1)
                        return True
                    except Exception as exc:
                        logger.warning("[phone-reg] SMS code 6-cell 失败,回退单 input: %s", exc)
                        break
        except Exception:
            continue

    # 单 input 模式 — JS focus + keyboard.type(不用 .fill 避免 actionability)
    for sel in CODE_INPUT_SELECTORS:
        try:
            visible = page.evaluate(
                """(s) => {
                    const el = document.querySelector(s);
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }""",
                sel,
            )
            if not visible:
                continue
            page.evaluate(
                """(s) => { const el = document.querySelector(s); if (el) el.focus(); }""",
                sel,
            )
            _sleep(0.15)
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
            except Exception:
                pass
            page.keyboard.type(code, delay=50)
            logger.info("[phone-reg] SMS code JS focus+type sel=%s", sel)
            return True
        except Exception:
            continue
    return False


def _fill_about_you(page: Page, full_name: str, birth: dict[str, str]) -> None:
    """填 about-you(姓名 + 年龄/生日)— 复刻 register.py 已有逻辑。

    注意:chatgpt.com 新版用 React Aria 的浮动 label(`_typeableLabel`),
    label 浮在 input 上方会拦截 Playwright 的 .click()。也注意 React Aria
    NumberField 跟纯 JS nativeSetter 不兼容(setter 把 "22" 设上去后,
    NumberField 的 onChange 把多位数当步进处理,最后只剩个位数)。
    所以策略是:JS focus(绕开 click 拦截)+ 真实键盘 type(NumberField 逐
    字符正确处理)。
    """
    _sleep(2)

    def _focus_and_type(selector: str, value: str) -> bool:
        """JS focus + 键盘 type,绕开浮动 label,兼容 React Aria NumberField。"""
        focused = page.evaluate(
            """(sel) => {
                const inp = document.querySelector(sel);
                if (!inp) return false;
                inp.focus();
                return document.activeElement === inp;
            }""",
            selector,
        )
        if not focused:
            return False
        _sleep(0.2)
        # 清空 — Ctrl+A 全选 + Delete,比逐字 Backspace 稳
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
        except Exception:
            pass
        page.keyboard.type(value, delay=50)
        _sleep(0.3)
        return True

    # 1) 姓名 — JS focus + type(绕开 React Aria 浮动 label)
    try:
        for sel in ('input[name="name"]',
                    'input[autocomplete="name"]',
                    'input[placeholder*="name" i]',
                    'input[placeholder*="姓名"]'):
            if _focus_and_type(sel, full_name):
                logger.info("[phone-reg] about-you 姓名: %s (focus+type via %s)",
                            full_name, sel)
                break
    except Exception as exc:
        logger.debug("[phone-reg] 姓名填写失败: %s", exc)

    # 2) 年龄(新版 React Aria NumberField)或生日 3 段(旧版 spinbutton)
    age_str = str(int(time.strftime("%Y")) - int(birth["year"]))
    try:
        # 先看 age input 是否存在(不 click,只看 DOM)
        age_exists = page.evaluate(
            """() => !!document.querySelector('input[name="age"]')"""
        )
        if age_exists:
            ok = _focus_and_type('input[name="age"]', age_str)
            # 校验实际值 — 如果 NumberField 把值改了(比如步进 bug),抓回来看看
            actual = page.evaluate(
                """() => (document.querySelector('input[name="age"]') || {}).value || ''"""
            )
            logger.info("[phone-reg] about-you 年龄: 设 %s 实际 %r (ok=%s)",
                        age_str, actual, ok)
            if str(actual) != age_str:
                # 再试一次:JS 完全清空 + 逐字符 type
                try:
                    page.evaluate(
                        """() => {
                            const inp = document.querySelector('input[name="age"]');
                            if (!inp) return;
                            inp.focus();
                            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                            setter.call(inp, '');
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                        }"""
                    )
                    _sleep(0.2)
                    page.keyboard.type(age_str, delay=120)
                    _sleep(0.3)
                    actual2 = page.evaluate(
                        """() => (document.querySelector('input[name="age"]') || {}).value || ''"""
                    )
                    logger.info("[phone-reg] about-you 年龄重试后: %r", actual2)
                except Exception as exc:
                    logger.warning("[phone-reg] 年龄重试异常: %s", exc)
        else:
            # 旧版 spinbutton — 按 aria-label 识别(年/月/日 或 year/month/day)
            spinbuttons = page.locator('[role="spinbutton"]')
            try:
                cnt = spinbuttons.count()
            except Exception:
                cnt = 0
            if cnt >= 3:
                logger.info("[phone-reg] about-you 用 spinbutton 生日填写 (%d)", cnt)
                # 旧版 spinbutton 也可能有 floating label 遮挡 — 用 force click
                def _click_sb(sb_locator):
                    try:
                        sb_locator.click(force=True, timeout=3000)
                    except Exception:
                        # 兜底 JS focus
                        try:
                            handle = sb_locator.element_handle(timeout=1000)
                            if handle:
                                page.evaluate("(el) => el.focus()", handle)
                        except Exception:
                            pass
                for i in range(cnt):
                    sb = spinbuttons.nth(i)
                    try:
                        label = (sb.get_attribute("aria-label") or "").lower()
                    except Exception:
                        label = ""
                    if "year" in label or "yyyy" in label or "年" in label:
                        _click_sb(sb)
                        _sleep(0.2)
                        page.keyboard.type(birth["year"], delay=60)
                    elif "month" in label or "mm" in label or "月" in label:
                        _click_sb(sb)
                        _sleep(0.2)
                        page.keyboard.type(birth["month"], delay=60)
                    elif "day" in label or "dd" in label or "日" in label:
                        _click_sb(sb)
                        _sleep(0.2)
                        page.keyboard.type(birth["day"], delay=60)
    except Exception as exc:
        logger.warning("[phone-reg] about-you 年龄/生日填写异常: %s", exc)

    # 失焦 + 勾选可能的同意 checkbox
    try:
        page.click("body")
    except Exception:
        pass
    _sleep(0.6)
    try:
        page.evaluate(
            """() => {
                for (const inp of document.querySelectorAll('input[type="checkbox"]')) {
                    if (inp.disabled || inp.checked) continue;
                    const r = inp.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const target = inp.closest('label') || inp;
                    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                        target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                    });
                    if (!inp.checked) {
                        inp.checked = true;
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
            }"""
        )
    except Exception:
        pass

    # 提交
    _click_submit_button(page)


# ─── Phase 1: chatgpt.com 注册阶段 ───────────────────────────────────────────

def _phase1_signup(
    page: Page,
    sms_provider,
    sms_cfg: dict,
    country: PhoneCountry,
    password: str,
    full_name: str,
    birth: dict[str, str],
) -> tuple[Any, str]:
    """完成 chatgpt.com 手机号注册。返回 (sms_order, phone_e164)。

    流程:
      1. 点「免费注册」(中英双语)
      2. 弹窗 →「继续使用手机登录」(中英双语)
      3. 选国家(默认从 SMS provider config 来)
      4. 买号(可换号 1-2 次)
      5. 填手机号 → 提交 → CF
      6. 等 SMS → 填 → 提交
      7. 密码 → 提交
      8. about-you → 提交
      9. 等 chatgpt.com 主页

    成功才返回。失败抛 RegisterFailed/RegisterBlocked,所有买过的号都已 cancel/ban。
    """
    logger.info("[phone-reg] === Phase 1 chatgpt.com 注册开始 ===")
    page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60000)
    safe_screenshot(page, SCREENSHOT_DIR / "phone_01_chatgpt_landing.png")

    cf_ok = wait_cloudflare(page, max_wait_seconds=90)
    if not cf_ok:
        raise RegisterFailed("Cloudflare 在 chatgpt.com 首页未通过(90s) — 可能 IP 信誉问题")
    _sleep(3)
    _dismiss_cookie_banner(page)

    def _signup_ready() -> str | None:
        """判定注册流程已就绪 — 任一信号命中即可:
        1) URL 已跳到 auth.openai.com(整页导航变体)
        2) 手机号输入框已渲染(极少见但作兜底)
        3) 手机登录按钮直接可见(新版 chatgpt.com 直接落「Log in or sign up」页)
        4) 弹窗/起始页文案出现
        返回命中的信号描述,None 表示尚未就绪。"""
        try:
            if "auth.openai.com" in (page.url or ""):
                return f"url->auth.openai.com ({page.url})"
        except Exception:
            pass
        try:
            if page.locator(PHONE_INPUT_SELECTOR).first.is_visible(timeout=300):
                return "phone-input visible"
        except Exception:
            pass
        try:
            has_phone_btn = page.evaluate(
                """(texts) => {
                    for (const b of document.querySelectorAll('button, [role="button"], a')) {
                        const t = (b.innerText || b.textContent || '').trim();
                        if (texts.some(tx => t.includes(tx))) return t;
                    }
                    return null;
                }""",
                list(PHONE_LOGIN_TEXTS),
            )
            if has_phone_btn:
                return f"phone-button visible: {has_phone_btn!r}"
        except Exception:
            pass
        if _wait_text_on_page(page, SIGNUP_MODAL_TEXTS, timeout_ms=500):
            return "modal text matched"
        return None

    url_before_click = page.url
    modal_ready_reason: str | None = None

    # 1) 优先检测:chatgpt.com 可能直接落在「Log in or sign up」页(新版,无 landing CTA),
    #    或已自动跳到 auth.openai.com — 这两种情况都不需要点「免费注册」。
    initial_sig = _signup_ready()
    if initial_sig:
        modal_ready_reason = initial_sig
        logger.info("[phone-reg] chatgpt.com 直接进入登录/注册页 — 跳过「免费注册」步骤,就绪信号: %s", initial_sig)
    else:
        # 老版 landing — 需要先点「免费注册」CTA
        if not _wait_button_by_text(page, SIGNUP_BUTTON_TEXTS, timeout_ms=30000):
            safe_screenshot(page, SCREENSHOT_DIR / "phone_01a_no_signup_button.png")
            # 诊断:dump 页面实际内容 — 区域限制 / Access denied / CF 五秒盾 / 异常 landing
            try:
                cur_url = page.url
                title = page.title()
                body_text = page.locator("body").inner_text(timeout=2000) or ""
                visible_buttons = page.evaluate(
                    """() => {
                        const out = [];
                        for (const b of document.querySelectorAll('button, [role="button"], a')) {
                            const t = (b.innerText || b.textContent || '').trim();
                            if (!t) continue;
                            const rect = b.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            out.push(t.slice(0, 80));
                            if (out.length >= 30) break;
                        }
                        return out;
                    }"""
                )
                logger.error(
                    "[phone-reg] [DIAG] phase1 landing 异常 | url=%s | title=%r | "
                    "visible_buttons=%s | body[:800]=%r",
                    cur_url, title, visible_buttons, (body_text or "")[:800],
                )
            except Exception as diag_exc:
                logger.error("[phone-reg] [DIAG] landing 诊断 dump 失败: %s", diag_exc)
            raise RegisterFailed(f"30s 内未见「{'/'.join(SIGNUP_BUTTON_TEXTS)}」按钮 — 页面可能没渲染好")
        # 等 React 事件处理器绑定(对照 JS 版本 5s)
        _sleep(5)

        for attempt in range(1, 4):
            if not _click_button_by_text(page, SIGNUP_BUTTON_TEXTS, timeout_ms=8000):
                logger.warning("[phone-reg] 第 %d 次点免费注册失败", attempt)
                _sleep(2)
                continue
            # 轮询 30s,任一就绪信号命中就过
            deadline = time.time() + 30
            while time.time() < deadline:
                sig = _signup_ready()
                if sig:
                    modal_ready_reason = sig
                    break
                time.sleep(0.5)
            if modal_ready_reason:
                logger.info("[phone-reg] 注册起始页就绪 (attempt=%d): %s", attempt, modal_ready_reason)
                break
            logger.warning("[phone-reg] 第 %d 次未检测到就绪信号 (url=%s)", attempt, page.url)
            _sleep(2)
    if not modal_ready_reason:
        safe_screenshot(page, SCREENSHOT_DIR / "phone_01b_no_modal.png")
        # dump 页面诊断信息便于排查
        try:
            body_text = page.locator("body").inner_text(timeout=2000) or ""
            visible_buttons = page.evaluate(
                """() => {
                    const out = [];
                    for (const b of document.querySelectorAll('button, [role="button"], a')) {
                        const t = (b.innerText || b.textContent || '').trim();
                        if (!t) continue;
                        const rect = b.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        out.push(t.slice(0, 80));
                        if (out.length >= 30) break;
                    }
                    return out;
                }"""
            )
            logger.error("[phone-reg] 页面诊断 url=%s | url_before_click=%s", page.url, url_before_click)
            logger.error("[phone-reg] 页面诊断 visible_buttons=%s", visible_buttons)
            logger.error("[phone-reg] 页面诊断 body[:600]=%r", (body_text or "")[:600])
        except Exception as diag_exc:
            logger.error("[phone-reg] 页面诊断失败: %s", diag_exc)
        raise RegisterFailed(
            "点免费注册 3 次后未检测到注册起始页就绪信号 — "
            "弹窗文案 / 跳转 / 手机按钮 / 手机输入框均未出现。"
            "请打开 phone_01b_no_modal.png 看页面实际状态,并把日志里 visible_buttons / body 文本片段贴出来"
        )

    safe_screenshot(page, SCREENSHOT_DIR / "phone_02_modal.png")
    _sleep(1)

    # 2) 点手机登录 — 中英文双语。新流程可能先要点「Log in or sign up to create」中间按钮,
    # 再才能看到「继续使用手机登录」入口,所以先 short-timeout 试一次,失败再走中间按钮兜底。
    if not _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=3000):
        # 兜底:看下是不是停在「Log in or sign up to create」中间页 — 点它再试
        if _click_button_by_text(page, INTERMEDIATE_LOGIN_TEXTS, timeout_ms=3000):
            logger.info("[phone-reg] 命中中间过渡按钮(Log in or sign up to create),继续找手机登录")
            _sleep(2)
            wait_cloudflare(page, max_wait_seconds=15)
            _sleep(1)
        if not _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=10000):
            safe_screenshot(page, SCREENSHOT_DIR / "phone_02a_no_phone_login.png")
            # dump 页面可见按钮诊断
            try:
                btns = page.evaluate(
                    """() => Array.from(document.querySelectorAll('button, [role="button"], a'))
                        .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                        .map(b => (b.innerText || b.textContent || '').trim())
                        .filter(t => t && t.length < 80).slice(0, 30)"""
                )
                logger.error("[phone-reg] 找不到手机登录按钮,页面 visible_buttons=%s url=%s",
                             btns, page.url)
            except Exception:
                pass
            raise RegisterFailed(f"找不到「{'/'.join(PHONE_LOGIN_TEXTS)}」按钮")

    # 3) 等手机号输入框
    try:
        page.locator(PHONE_INPUT_SELECTOR).first.wait_for(state="visible", timeout=15000)
    except Exception as exc:
        safe_screenshot(page, SCREENSHOT_DIR / "phone_02b_no_input.png")
        raise RegisterFailed(f"等待手机号输入框超时: {exc}") from exc
    safe_screenshot(page, SCREENSHOT_DIR / "phone_03_phone_form.png")

    # 4) 选国家
    _select_country(page, country)
    _sleep(1)

    # 5) 买号 → 填 → 提交,死号/拒收时 ban 退款换号
    last_err: Exception | None = None
    used_order_ids: list[int] = []
    for attempt in range(1, MAX_SMS_BUY_ATTEMPTS + 1):
        if is_stop_requested():
            raise BatchStopped("phone reg phase1 收到 stop")

        try:
            order = _buy_phone_order(sms_provider, sms_cfg, attempt)
        except sms_mod.SmsBuyFailed as exc:
            last_err = exc
            logger.warning("[phone-reg] 买号失败 attempt=%d/%d: %s",
                           attempt, MAX_SMS_BUY_ATTEMPTS, exc)
            _sleep(5)
            continue
        except sms_mod.SmsConfigMissing:
            raise
        if order.id in used_order_ids:
            logger.warning("[phone-reg] provider 给了重复号 id=%d phone=%s,放弃", order.id, order.phone)
            _safe_refund(sms_provider, order, "cancel")
            last_err = RegisterBlocked("phone_reg", "SMS provider 反复给同一号", is_phone=True)
            continue
        used_order_ids.append(order.id)

        phone_e164 = order.phone or ""
        local_number = strip_dial_prefix(phone_e164, country)
        logger.info("[phone-reg] attempt=%d order=%d phone=%s local=%s",
                    attempt, order.id, phone_e164, local_number)

        # attempt>1 时 page 可能停在上一轮的密码页/SMS code 页 — 恢复到手机输入态
        if not _back_to_phone_input(page, country):
            logger.warning("[phone-reg] page 状态无法恢复到手机输入态 attempt=%d", attempt)
            _safe_refund(sms_provider, order, "cancel")
            last_err = RegisterFailed("page 状态恢复失败")
            continue

        # 每个 attempt 都强制重选国家 — Edit 回手机输入页时国家可能被前次提交重置回 US,
        # 导致 +55 巴西号被填成 +1 美国格式触发「Phone number is not valid」死循环
        try:
            _select_country(page, country)
            _sleep(0.6)
        except Exception as exc:
            logger.warning("[phone-reg] attempt=%d 重选国家异常(继续): %s", attempt, exc)

        try:
            _fill_phone_input(page, local_number, phone_e164, country)
        except Exception as exc:
            logger.warning("[phone-reg] 填手机号失败 attempt=%d: %s", attempt, exc)
            _safe_refund(sms_provider, order, "cancel")  # 未触发 SMS
            last_err = exc
            continue

        # 检测「号码已使用」冲突
        conflict = _detect_phone_conflict(page)
        if conflict:
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_04_conflict_a{attempt}.png")
            logger.warning("[phone-reg] 手机号冲突 attempt=%d: %s", attempt, conflict)
            _safe_refund(sms_provider, order, "ban")  # 死号:别人占了
            last_err = RegisterBlocked("phone_reg",
                                       f"号 {phone_e164} 已被注册: {conflict}", is_phone=True)
            continue

        # 点继续提交手机号
        if not _click_submit_button(page):
            logger.warning("[phone-reg] 提交手机号按钮点击失败 attempt=%d", attempt)
            _safe_refund(sms_provider, order, "cancel")
            last_err = RegisterFailed("提交手机号点击失败")
            continue
        _sleep(3)
        wait_cloudflare(page, max_wait_seconds=60)
        _sleep(3)

        # 提交后再检测冲突(后端校验慢)
        conflict = _detect_phone_conflict(page)
        if conflict:
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_04b_conflict_after_submit_a{attempt}.png")
            logger.warning("[phone-reg] 提交后号码冲突 attempt=%d: %s", attempt, conflict)
            _safe_refund(sms_provider, order, "ban")
            last_err = RegisterBlocked("phone_reg",
                                       f"号 {phone_e164} 提交后被拒: {conflict}", is_phone=True)
            continue

        # 检测「号码无效」前端校验文案 — 通常是国家代码选错(_select_country 失败) +
        # _fill_phone_input 兜底也没生效。SMS 永远不会到,立即 ban 退款换号。
        invalid_msg = _detect_phone_invalid(page)
        if invalid_msg:
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_04c_invalid_a{attempt}.png")
            logger.warning("[phone-reg] 提交后号码无效 attempt=%d: %s (phone=%s country=+%s)",
                           attempt, invalid_msg, phone_e164, country.dial_code)
            # 诊断:dump 当前国家显示 + 输入框值
            try:
                diag = page.evaluate(
                    """() => {
                        let triggerText = '';
                        for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
                            const r = b.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            triggerText = (b.innerText || '').trim(); break;
                        }
                        const inp = document.querySelector('input[name="phoneNumberInput"], input[type="tel"]');
                        return { trigger: triggerText, inputValue: inp ? inp.value : '' };
                    }"""
                )
                logger.error("[phone-reg] 号码无效诊断 trigger=%r inputValue=%r 目标=%s 期望dial=+%s",
                             diag.get("trigger"), diag.get("inputValue"),
                             country.cn_name, country.dial_code)
            except Exception:
                pass
            _safe_refund(sms_provider, order, "ban")
            last_err = RegisterBlocked(
                "phone_reg",
                f"号 {phone_e164} 被前端拒「{invalid_msg}」 — 可能国家代码未选对(dial=+{country.dial_code})",
                is_phone=True,
            )
            continue

        safe_screenshot(page, SCREENSHOT_DIR / f"phone_05_after_phone_submit_a{attempt}.png")

        # 等 SMS code 输入框
        sms_visible = False
        for _ in range(15):
            try:
                for sel in CODE_INPUT_SELECTORS:
                    if page.locator(sel).first.is_visible(timeout=600):
                        sms_visible = True
                        break
                if sms_visible:
                    break
            except Exception:
                pass
            _sleep(1)

        if not sms_visible:
            # SMS code 框 15s 未出现 — 检测页面是否已跳到「创建密码」/ about-you / 主页等
            # 后续步骤(用户描述的真实流程:填手机号 → 提交 → 创建密码页 → 填密码
            # → Continue → 这才触发 OpenAI 发 SMS → 验证页)
            try:
                is_pw_page = page.locator(PASSWORD_INPUT_SELECTOR).first.is_visible(timeout=2000)
            except Exception:
                is_pw_page = False
            try:
                cur_url = (page.url or "").lower()
            except Exception:
                cur_url = ""
            # body 文本兜底 — selector 被 React Aria overlay 拦截时仍能识别
            try:
                cur_body = (page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 500)") or "")
            except Exception:
                cur_body = ""
            # 密码页变体:"Create your password" = 新号,"Enter your password" + "Forgot password?" = 已注册
            pw_kind = _classify_password_page(page) if is_pw_page else "unknown"
            # /log-in/password URL OR body 文本是「Enter your password」/「Forgot password?」 → 已存在号
            on_existing_login = ("/log-in/password" in cur_url) or (pw_kind == "existing")
            on_about_you = ("about-you" in cur_url) or ("about_you" in cur_url)
            on_main = ("chatgpt.com" in cur_url and "auth.openai.com" not in cur_url
                       and "/auth/" not in cur_url)
            # 「Create your password」体也算密码页(即使 input selector 被 overlay 拦截)
            if not is_pw_page and pw_kind == "create":
                logger.info("[phone-reg] [DIAG] password selector 不可见但 body 含「Create your password」 — 视为新号密码页")
                is_pw_page = True

            if on_existing_login:
                logger.warning(
                    "[phone-reg] 号 %s 被识别为已存在账号(url=%s pw_kind=%s)— ban 换号 attempt=%d",
                    phone_e164, cur_url[:80], pw_kind, attempt,
                )
                safe_screenshot(page, SCREENSHOT_DIR / f"phone_05c_existing_account_a{attempt}.png")
                _safe_refund(sms_provider, order, "ban")
                last_err = RegisterBlocked(
                    "phone_reg",
                    f"号 {phone_e164} 已被注册过(Enter your password / Forgot password 链接出现)",
                    is_phone=True,
                )
                continue

            if is_pw_page or on_about_you or on_main:
                logger.info(
                    "[phone-reg] SMS 框未出现但已进入后续步骤(pw=%s about-you=%s main=%s)"
                    " — break 让主循环接管 attempt=%d url=%s",
                    is_pw_page, on_about_you, on_main, attempt, cur_url[:80],
                )
                safe_screenshot(page, SCREENSHOT_DIR / f"phone_05b_skipped_sms_a{attempt}.png")
                break  # 跳出 SMS retry 循环,进 phase1 主循环
            else:
                logger.warning(
                    "[phone-reg] 提交手机号后 15s 未见 code 框也未见后续页 — 号可能被拒 "
                    "url=%s body[:160]=%r",
                    cur_url[:120], cur_body[:160],
                )
                safe_screenshot(page, SCREENSHOT_DIR / f"phone_06_no_code_input_a{attempt}.png")
                _safe_refund(sms_provider, order, "ban")
                last_err = RegisterBlocked("phone_reg",
                                           "提交手机号后未出现 SMS 输入框", is_phone=True)
                continue

        # 等 SMS
        logger.info("[phone-reg] 等 SMS#1 attempt=%d order=%d", attempt, order.id)
        try:
            code = sms_provider.wait_for_otp(
                order_id=order.id, timeout=SMS_WAIT_SECONDS,
                should_stop=is_stop_requested,
            )
        except sms_mod.SmsAborted:
            _safe_refund(sms_provider, order, "cancel")
            raise BatchStopped("phone reg phase1 等 SMS 时收到 stop")
        except (sms_mod.SmsTimeout, sms_mod.SmsError) as exc:
            # SmsError 通常是 STATUS_CANCEL(hero-sms 服务端超时自动 cancel) — 跟 timeout 同样处理
            logger.warning("[phone-reg] SMS#1 失败 attempt=%d (%s) — 死号 ban 重试",
                           attempt, type(exc).__name__)
            _safe_refund(sms_provider, order, "ban")
            last_err = exc
            # 重新填手机号:刷新页面?直接返回 phone 输入框?
            # 简单起见:goto 重新开始
            try:
                page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60000)
                wait_cloudflare(page, max_wait_seconds=90)
                _sleep(2)
                if _click_button_by_text(page, SIGNUP_BUTTON_TEXTS, timeout_ms=8000):
                    _wait_text_on_page(page, SIGNUP_MODAL_TEXTS, timeout_ms=15000)
                    _sleep(1)
                    _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=8000)
                    page.locator(PHONE_INPUT_SELECTOR).first.wait_for(state="visible", timeout=15000)
                    _select_country(page, country)
            except Exception as exc2:
                logger.warning("[phone-reg] 刷新重新进表单失败: %s", exc2)
            continue

        # 填 SMS code
        if not _fill_sms_code_smart(page, code):
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_07_code_fill_failed_a{attempt}.png")
            logger.warning("[phone-reg] SMS code 填写失败 (code=%s)", code)
            try:
                sms_provider.cancel_order(order.id)  # SMS 已到,不退
            except Exception:
                pass
            last_err = RegisterBlocked("phone_reg", "SMS code 填写失败", is_phone=True)
            continue

        _click_submit_button(page)
        _sleep(3)
        wait_cloudflare(page, max_wait_seconds=60)
        _sleep(2)

        # 检测 code 是否被拒(回到 phone 页 / 还在 code 页)
        try:
            still_code = any(
                page.locator(s).first.is_visible(timeout=500) for s in CODE_INPUT_SELECTORS
            )
        except Exception:
            still_code = False
        if still_code:
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_07b_code_rejected_a{attempt}.png")
            logger.warning("[phone-reg] SMS code 提交后仍在 code 页 — 拒收/错码")
            try:
                sms_provider.cancel_order(order.id)
            except Exception:
                pass
            last_err = RegisterBlocked("phone_reg",
                                       "SMS code 提交后被拒(可能 OpenAI 风控)", is_phone=True)
            continue

        # 成功:进入密码/about-you/主页阶段。这里 order 保留(还要 phase2 再用一次 SMS)
        logger.info("[phone-reg] ✅ Phase 1 SMS#1 验证通过 order=%d", order.id)
        safe_screenshot(page, SCREENSHOT_DIR / "phone_08_after_sms.png")
        break
    else:
        # 重试次数耗尽
        raise RegisterBlocked(
            "phone_reg",
            f"手机号注册 {MAX_SMS_BUY_ATTEMPTS} 次都失败: {last_err}",
            is_phone=True,
        )

    # 6) 后续:密码 / about-you 循环
    last_url = ""
    # 诊断计数:Phase 1 是否真的走过密码页 / about-you 等
    diag_counts = {"pw_hit": 0, "about_you_hit": 0, "fallback_hit": 0, "rounds": 0}
    # 每号(每个 order)是否已主动点过 Resend — SMS#2 经验:OpenAI 这一步若不主动点
    # Resend,provider 可能永远等不到下一条 SMS。所以进 SMS 页等 15s 没收到就点 Resend。
    resend_clicked_for: set[int] = set()
    # 主循环里 SMS 验证页换号次数(场景:phone → 密码 → about-you → SMS code,
    # 这种 SMS 验证是在主循环里等的,跟上面的 SMS retry 循环分开计数)
    main_sms_attempts = 0
    for round_idx in range(20):
        if is_stop_requested():
            raise BatchStopped("phone reg phase1 完成资料阶段收到 stop")

        _sleep(3)
        diag_counts["rounds"] = round_idx + 1
        try:
            url = (page.url or "").lower()
            inputs_info = page.evaluate(
                """() => Array.from(document.querySelectorAll('input:not([type=hidden])')).map(i => ({
                    type: i.type, name: i.name, placeholder: i.placeholder || '',
                }))"""
            )
            body_text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 400)")
        except Exception:
            logger.debug("[phone-reg] phase1 r%d page eval 失败", round_idx)
            last_url = ""
            continue

        # 诊断:每轮记录 url + input 摘要(只有 url 变化才打印,避免刷屏)+ 每次 URL 变化都拍一张
        if url != last_url:
            input_types = [i.get("type") for i in inputs_info if i.get("type")]
            logger.info(
                "[phone-reg] [DIAG] phase1 r%d url=%s inputs=%s body[:120]=%r",
                round_idx, url[:120], input_types[:8], (body_text or "")[:120],
            )
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_main_r{round_idx:02d}_state.png")

        # 完成:落 chatgpt.com 主页
        if "chatgpt.com" in url and "auth.openai.com" not in url and "/auth/" not in url:
            logger.info(
                "[phone-reg] ✅ Phase 1 完成,落 chatgpt.com 主页: %s | "
                "[DIAG] phase1 终态汇总 rounds=%d pw_hit=%d about_you_hit=%d fallback_hit=%d",
                url, diag_counts["rounds"], diag_counts["pw_hit"],
                diag_counts["about_you_hit"], diag_counts["fallback_hit"],
            )
            if diag_counts["pw_hit"] == 0:
                logger.warning(
                    "[phone-reg] [DIAG] ⚠️ Phase 1 全程未命中密码页 — 账号可能没有密码,"
                    "Phase 2 若出现 /log-in/password 不能用 password 参数填(会被判为密码错)"
                )
            safe_screenshot(page, SCREENSHOT_DIR / "phone_09_chatgpt_landing.png")
            return order, phone_e164

        # ★ 先识别 SMS#2 验证页(URL 可能含 "password" 如 /create-account/password/sms-verify,
        # 但 body 已是「Check your phone」)。识别成功 → is_pw_page 必为 False。
        body_low = (body_text or "").lower()
        is_sms_body = (
            "check your phone" in body_low
            or "enter the verification" in body_low
            or "verification code" in body_low
            or "请输入验证码" in (body_text or "")
            or "输入验证码" in (body_text or "")
        )
        has_pw_input = any(i.get("type") == "password" for i in inputs_info)
        # is_pw_page 必须 (有 type=password 输入框) OR (URL 含 password 且 body 有密码字眼);
        # 且 body 不是 SMS 验证页。仅 URL 字符串不再算密码页 — 避免把 SMS#2 误判。
        url_says_pw = ("password" in url) and ("password" in body_low or "密码" in (body_text or ""))
        is_pw_page = (not is_sms_body) and (has_pw_input or url_says_pw)
        if last_url == url and not is_pw_page and not is_sms_body:
            continue

        # 密码页分类(主循环必走) — URL /log-in/password OR body 含「Enter your password」/
        # 「Forgot password?」 → 已注册号,必须 ban 换号;否则视为 Create your password(新号)
        pw_kind = _classify_password_page(page) if is_pw_page else "unknown"
        if "/log-in/password" in url.lower() or pw_kind == "existing":
            logger.warning(
                "[phone-reg] phase1 r%d 主循环命中已注册号密码页(url=%s pw_kind=%s)— 号 %s,ban 换号",
                round_idx, url[:80], pw_kind, phone_e164,
            )
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_main_existing_account_r{round_idx}.png")
            _safe_refund(sms_provider, order, "ban")
            raise RegisterBlocked(
                "phone_reg",
                f"主循环检测到已注册号密码页(Enter your password / Forgot password)— 号 {phone_e164} 已被注册过",
                is_phone=True,
            )

        if is_pw_page:
            # ★ 决策时的 body 可能是 stale(transitional state / 空字符串)→ is_sms_body=False
            # 误判为密码页。立刻再 query 一次实时状态:若已是 SMS 页 或 没 password input,
            # 跳过这一轮(不浪费 15s 在 _fill_password_input,也不把密码 type 进 Code input)
            try:
                live_check = page.evaluate(
                    """() => ({
                        has_pw: !!document.querySelector('input[type="password"]'),
                        is_sms: ['check your phone','enter the verification','verification code','请输入验证码','输入验证码']
                            .some(k => (document.body && document.body.innerText || '').toLowerCase().includes(k.toLowerCase())),
                    })"""
                )
            except Exception:
                live_check = {"has_pw": True, "is_sms": False}
            if live_check.get("is_sms") or not live_check.get("has_pw"):
                logger.info(
                    "[phone-reg] phase1 r%d 入 is_pw_page 块但 live 重检发现已不是密码页 "
                    "(has_pw=%s is_sms=%s) — skip 让下一轮重判",
                    round_idx, live_check.get("has_pw"), live_check.get("is_sms"),
                )
                last_url = url
                continue

            diag_counts["pw_hit"] += 1
            # 同一 URL 上连续填密码 > 3 次 = 卡死(Continue 没起作用),立刻 abort
            # 不能让主循环傻等 20 轮 × 65s ≈ 22min
            if last_url == url and diag_counts["pw_hit"] >= 3:
                safe_screenshot(page, SCREENSHOT_DIR / f"phone_pw_stuck_r{round_idx}.png")
                logger.error(
                    "[phone-reg] phase1 r%d 密码页连续 %d 次同 URL 都没跳走 — Continue 点击无效,abort",
                    round_idx, diag_counts["pw_hit"],
                )
                raise RegisterFailed(
                    f"创建密码页卡死({diag_counts['pw_hit']} 次同 URL) — Continue 按钮无响应"
                )
            logger.info(
                "[phone-reg] [DIAG] phase1 r%d 命中创建密码页(#%d) url=%s pw_kind=%s",
                round_idx, diag_counts["pw_hit"], url[:80], pw_kind,
            )
            # 1. 填密码(JS focus + keyboard.type,React Aria 兼容)
            _fill_password_input(page, password)
            _sleep(0.5)
            # 2. 等 Continue 按钮启用(密码异步校验) — 5s 足够
            _wait_continue_enabled(page, max_wait_s=5)
            # 3. 双重提交:先 Enter 键(OpenAI 表单普遍支持),再点 Continue 兜底
            try:
                page.keyboard.press("Enter")
                logger.info("[phone-reg] phase1 r%d 密码已填,按 Enter 提交", round_idx)
            except Exception:
                pass
            _sleep(1)
            clicked = _click_submit_button(page)
            if not clicked:
                logger.warning("[phone-reg] phase1 r%d Continue 按钮没找到 / 全 disabled — 靠 Enter 兜底", round_idx)
            _sleep(3)
            # 检测是否真的跳走了 — URL 短时间内变化才算成功提交
            url_after = ""
            for _ in range(10):  # 等最多 10s
                try:
                    url_after = (page.url or "").lower()
                except Exception:
                    url_after = ""
                if url_after and url_after != url:
                    logger.info("[phone-reg] phase1 r%d 密码提交后已跳走 → %s", round_idx, url_after[:80])
                    break
                _sleep(1)
            else:
                logger.warning(
                    "[phone-reg] phase1 r%d 密码提交后 10s 内 URL 没变 — 可能 Continue 没生效或后端慢",
                    round_idx,
                )
                safe_screenshot(page, SCREENSHOT_DIR / f"phone_main_pw_no_advance_r{round_idx}.png")
                # 诊断:dump 当前所有按钮 + 错误文案,帮排查为啥没跳走
                try:
                    diag_btns = page.evaluate(
                        """() => Array.from(document.querySelectorAll('button')).map(b => ({
                            text: (b.innerText || '').trim().slice(0, 40),
                            type: b.getAttribute('type') || '',
                            disabled: b.disabled,
                            ariaLabel: b.getAttribute('aria-label') || '',
                        }))"""
                    )
                    diag_body = page.evaluate(
                        "() => (document.body && document.body.innerText || '').slice(0, 400)"
                    )
                    logger.warning(
                        "[phone-reg] [DIAG] 卡密码页 r%d 全量按钮 dump: %s | body[:400]=%r",
                        round_idx, diag_btns[:20], diag_body[:400],
                    )
                except Exception:
                    pass
            wait_cloudflare(page, max_wait_seconds=30)
            _sleep(2)
            last_url = url
            continue

        # about-you
        if "about-you" in url or "about_you" in url or "确认一下你的年龄" in body_text:
            diag_counts["about_you_hit"] += 1
            logger.info(
                "[phone-reg] [DIAG] phase1 r%d 命中 about-you(#%d) url=%s",
                round_idx, diag_counts["about_you_hit"], url[:80],
            )
            _fill_about_you(page, full_name, birth)
            _sleep(5)
            wait_cloudflare(page, max_wait_seconds=60)
            _sleep(2)
            last_url = url
            continue

        # SMS code 输入页(密码 / about-you 之后才触发的 SMS 验证场景)
        is_sms_page = ("contact-verification" in url or "phone-verification" in url
                       or "verify" in url) or any(
            (i.get("type") in ("text", "tel", "number") and i.get("name") != "phoneNumberInput"
             and ("code" in (i.get("name") or "").lower()
                  or "code" in (i.get("placeholder") or "").lower()
                  or any(s in (i.get("placeholder") or "").lower()
                         for s in ("verification", "验证"))))
            for i in inputs_info
        )
        if is_sms_page:
            logger.info("[phone-reg] phase1 r%d 命中 SMS 验证页 order=%d — 拍照 + 等 15s 后若无码点 Resend",
                        round_idx, order.id)
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_main_sms_enter_r{round_idx}.png")
            # 策略:先等 15s 看 OpenAI 自动发的 SMS#2 来不来 → 没来则点 Resend,再等剩余 105s
            initial_wait = 15
            code = None
            try:
                code = sms_provider.wait_for_otp(
                    order_id=order.id, timeout=initial_wait,
                    should_stop=is_stop_requested,
                )
            except sms_mod.SmsAborted:
                raise BatchStopped("phone reg phase1 SMS 等待时收到 stop")
            except (sms_mod.SmsTimeout, sms_mod.SmsError):
                # SmsError(STATUS_CANCEL)和 timeout 一样进 Resend 流程
                # 没收到 → 点 Resend(每号只点一次)
                if order.id not in resend_clicked_for:
                    try:
                        clicked = page.evaluate(
                            """() => {
                                const btns = document.querySelectorAll(
                                    'button, a, [role="button"]'
                                );
                                for (const b of btns) {
                                    const t = (b.innerText || '').trim().toLowerCase();
                                    if (t.includes('resend') || t.includes('重新发送') || t.includes('重发')) {
                                        b.click();
                                        return t.slice(0, 60);
                                    }
                                }
                                return '';
                            }"""
                        )
                        if clicked:
                            logger.info("[phone-reg] phase1 r%d ✅ 点 Resend(%s) order=%d",
                                        round_idx, clicked, order.id)
                            resend_clicked_for.add(order.id)
                            safe_screenshot(
                                page, SCREENSHOT_DIR / f"phone_main_sms_resend_r{round_idx}.png",
                            )
                        else:
                            logger.warning("[phone-reg] phase1 r%d Resend 按钮没找到", round_idx)
                    except Exception as exc:
                        logger.warning("[phone-reg] phase1 r%d 点 Resend 异常: %s", round_idx, exc)
                else:
                    logger.info("[phone-reg] phase1 r%d order=%d Resend 已点过,继续等",
                                round_idx, order.id)
                # 继续等剩余预算(SMS_WAIT_SECONDS - initial_wait = 105s)
                try:
                    code = sms_provider.wait_for_otp(
                        order_id=order.id,
                        timeout=max(SMS_WAIT_SECONDS - initial_wait, 30),
                        should_stop=is_stop_requested,
                    )
                except sms_mod.SmsAborted:
                    raise BatchStopped("phone reg phase1 SMS 等待时收到 stop")
                except (sms_mod.SmsTimeout, sms_mod.SmsError) as exc:
                    # SmsError(hero-sms STATUS_CANCEL,长时间没码服务端自动取消)
                    # 跟真 timeout 一样进 ban 换号流程,而不是抛 SmsError 当未预期异常
                    code = None
                    sms_timeout_exc = exc
            if code is None:
                exc = locals().get("sms_timeout_exc")
                safe_screenshot(page, SCREENSHOT_DIR / f"phone_main_sms_timeout_r{round_idx}.png")
                main_sms_attempts += 1
                logger.warning(
                    "[phone-reg] phase1 r%d 主循环 SMS 超时 %ds order=%d — ban 换号 "
                    "(主循环换号 %d/%d)",
                    round_idx, SMS_WAIT_SECONDS, order.id,
                    main_sms_attempts, MAX_SMS_BUY_ATTEMPTS,
                )
                _safe_refund(sms_provider, order, "ban")
                if main_sms_attempts >= MAX_SMS_BUY_ATTEMPTS:
                    raise RegisterBlocked(
                        "phone_reg",
                        f"主循环 SMS 超时 {MAX_SMS_BUY_ATTEMPTS} 次都没收到: {exc}",
                        is_phone=True,
                    ) from exc
                # 换号:回退到手机号输入态 → 买新号 → 填 → 提交 → 等 SMS 框 → 主循环继续
                try:
                    if not _back_to_phone_input(page, country):
                        raise RegisterBlocked(
                            "phone_reg",
                            "SMS 超时后无法回退到手机号输入态",
                            is_phone=True,
                        )
                    new_order = _buy_phone_order(
                        sms_provider, sms_cfg, attempt_idx=main_sms_attempts + 100,
                    )
                    if new_order.id in used_order_ids:
                        _safe_refund(sms_provider, new_order, "cancel")
                        raise RegisterBlocked(
                            "phone_reg", "provider 反复给同号", is_phone=True,
                        )
                    used_order_ids.append(new_order.id)
                    new_e164 = (new_order.phone or "").strip()
                    if new_e164 and not new_e164.startswith("+"):
                        new_e164 = "+" + new_e164
                    new_local = strip_dial_prefix(new_e164, country)
                    logger.info(
                        "[phone-reg] phase1 主循环换号成功 new_order=%d phone=%s",
                        new_order.id, new_e164,
                    )
                    _select_country(page, country)
                    _sleep(1)
                    _fill_phone_input(page, new_local, new_e164, country)
                    if not _click_submit_button(page):
                        _safe_refund(sms_provider, new_order, "cancel")
                        raise RegisterBlocked(
                            "phone_reg", "换号后提交按钮点击失败", is_phone=True,
                        )
                    _sleep(3)
                    wait_cloudflare(page, max_wait_seconds=60)
                    _sleep(2)
                    order = new_order
                    phone_e164 = new_e164
                    last_url = ""  # 重置避免下一轮主循环 dedupe 跳过
                    continue
                except RegisterBlocked:
                    raise
                except Exception as e2:
                    logger.exception("[phone-reg] 主循环换号过程异常")
                    raise RegisterBlocked(
                        "phone_reg", f"主循环换号异常: {e2}", is_phone=True,
                    ) from e2

            if not _fill_sms_code_smart(page, code):
                safe_screenshot(page, SCREENSHOT_DIR / f"phone_07_main_sms_fill_failed_r{round_idx}.png")
                raise RegisterBlocked("phone_reg", "主循环 SMS code 填写失败", is_phone=True)
            _click_submit_button(page)
            _sleep(4)
            wait_cloudflare(page, max_wait_seconds=60)
            _sleep(2)
            last_url = url
            continue

        # 通用「同意 / 继续」类按钮兜底
        for btn_texts in (("同意", "Agree", "Accept", "I'm okay", "好的", "确定"),
                          SUBMIT_BUTTON_TEXTS):
            if _click_button_by_text(page, btn_texts, timeout_ms=1500):
                diag_counts["fallback_hit"] += 1
                logger.info(
                    "[phone-reg] [DIAG] phase1 r%d 兜底点击命中(#%d) url=%s btn_set=%s",
                    round_idx, diag_counts["fallback_hit"], url[:80], btn_texts[:3],
                )
                break

    safe_screenshot(page, SCREENSHOT_DIR / "phone_10_phase1_timeout.png")
    logger.error(
        "[phone-reg] [DIAG] phase1 超时汇总 rounds=%d pw_hit=%d about_you_hit=%d fallback_hit=%d last_url=%s",
        diag_counts["rounds"], diag_counts["pw_hit"],
        diag_counts["about_you_hit"], diag_counts["fallback_hit"], page.url,
    )
    raise RegisterFailed(f"Phase 1 完成资料阶段超时 (20 轮),最后 url={page.url}")


def _fill_add_email(page: Page, email: str) -> None:
    """填 /add-email 的 email 输入框 — JS focus + keyboard.type 避开 React Aria 拦截。

    抛 OAuthFailed 如果找不到 input 或填写失败。"""
    deadline = time.monotonic() + 5
    ei_sel = ('input[type="email"], input[name="email"], '
              'input[name="username"], input[name="identifier"]')
    ready = False
    while time.monotonic() < deadline:
        if page.evaluate(f"() => !!document.querySelector('{ei_sel}')"):
            ready = True
            break
        _sleep(0.3)
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
    _sleep(0.2)
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    except Exception:
        pass
    page.keyboard.type(email, delay=30)
    _sleep(0.3)


# ─── Phase 1.5: 注册后立即绑邮箱(auth.openai.com/add-email) ──────────────────
#
# 为什么需要这步:phase 1 注册完只有 phone+password,账号未绑邮箱。phase 2 OAuth
# 时,OpenAI 会发个 callback code,但因为账号不完整(没邮箱),token 交换返
# token_exchange_user_error。所以 phase 1 完成、phase 2 之前,必须先把邮箱绑上。
#
# 用 phase 1 同一个 context(带 cookies),直接 navigate 到
# https://auth.openai.com/add-email。OpenAI 看 session 是登录态,直接进绑邮箱页;
# 若被重定向到 /log-in,补一次密码登录(phone+password 都有)即可。

def _phase15_bind_email(
    page: Page,
    *,
    email_for_bind: str,
    address_id_for_bind: int | None,
    mail_client,
    password: str,
    phone_e164: str,
    country: PhoneCountry,
    auth_url: str,
) -> tuple[bool, str, int | None]:
    """phase 1 完成后立即绑邮箱。返回 (ok, bound_email, bound_address_id)。

    - ok=True:邮箱绑成功(bound_email 等于传入,签名保留是为兼容老调用方)
    - ok=False:放弃(不抛),让 phase 2 fallback;号最终走 pending,可手动补绑

    设计:不抛 OAuthFailed — 即便绑邮箱失败,phase 2 也可以尝试(顶多再失败一次,
    号当 phone-only pending 入库,用户可以走「补绑邮箱」按钮重试)。这步是
    best-effort,不阻塞主流程。

    单次尝试,60s 超时直接放弃。

    ★ v2 (2026-05-25):入口 URL 从 /add-email 改成 OAuth URL(同 phase 2)。
    原因:OpenAI 的 /add-email/send 后端校验 session 必须最近做过 password/verify
    (用户手动对比验证),否则返 200 但 silent drop 不发邮件。
    走 OAuth URL → /log-in → 填 phone → /log-in/password → 填密码 → POST
    /password/verify → 自动落 /add-email → submit → 邮件秒到。
    完全模仿用户手动操作的请求链。
    """
    OTP_TIMEOUT_S = 60          # 60s 收不到就放弃,phase 2 兜底

    current_email = email_for_bind
    current_address_id = address_id_for_bind
    logger.info("[phone-reg] === Phase 1.5 绑邮箱(v2 经 OAuth)=== email=%s", current_email)
    safe_screenshot(page, SCREENSHOT_DIR / "phone_15_pre_bind_email.png")

    # cloud-mail baseline:绑后 OTP 不抓老邮件
    try:
        mail_baseline_id = mail_client.latest_mail_id(current_email)
    except Exception as exc:
        logger.warning("[phone-reg] phase1.5 取 cloud-mail baseline 失败(继续): %s", exc)
        mail_baseline_id = None

    # ★ 不直 goto /add-email — 必须走 OAuth URL 触发 password_verify
    # 用传入的 auth_url(top-level 已生成 PKCE + state)— phase 1.5 走完 OAuth 拿到
    # 的 callback code 跟 top-level 的 verifier 是一对,直接换 token 就能成功,
    # 不需要 phase 2 再跑一遍。
    logger.info("[phone-reg] phase1.5 入口 OAuth URL(共用 top-level PKCE)")
    try:
        page.goto(auth_url, wait_until="load", timeout=30000)
        _sleep(2)
        wait_cloudflare(page, max_wait_seconds=30)
    except Exception as exc:
        logger.warning("[phone-reg] phase1.5 goto OAuth URL 失败(放弃): %s", exc)
        return False, current_email, current_address_id

    safe_screenshot(page, SCREENSHOT_DIR / "phone_15a_add_email_landing.png")

    email_submitted = False
    otp_submitted = False
    last_url = ""
    max_rounds = 15
    for round_idx in range(max_rounds):
        if is_stop_requested():
            logger.info("[phone-reg] phase1.5 收到 stop,放弃绑邮箱")
            return False, current_email, current_address_id

        _sleep(2)
        try:
            url = (page.url or "")
            url_low = url.lower()
            inputs_info = page.evaluate(
                """() => Array.from(document.querySelectorAll('input:not([type=hidden])')).map(i => ({
                    type: i.type, name: i.name, placeholder: i.placeholder || '',
                }))"""
            )
            body_text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 500)")
        except Exception:
            logger.debug("[phone-reg] phase1.5 r%d eval 失败", round_idx)
            last_url = ""
            continue

        if url != last_url:
            input_types = [i.get("type") for i in inputs_info if i.get("type")]
            logger.info(
                "[phone-reg] [DIAG] phase1.5 r%d url=%s inputs=%s body[:160]=%r",
                round_idx, url[:120], input_types[:8], (body_text or "")[:160],
            )
            safe_screenshot(page, SCREENSHOT_DIR / f"phone_15_bind_r{round_idx:02d}.png")

        # 成功:必须走到 OAuth callback URL — 这才能让顶层抓到 auth_code 换 token
        # - localhost:1455/auth/callback?code=... → OAuth 完成
        # - 落回 chatgpt.com(罕见,codex OAuth 走 localhost 不走 chatgpt)
        #
        # 之前的"otp_submitted 且不在 add-email/email-verification"分支去掉了 —
        # OTP 提交后会跳 consent 页(auth.openai.com/oauth/authorize?...),那个 URL
        # 不在 add-email 也不在 email-verification → 误判提前 return,导致 consent
        # 没点 → callback 没触发 → 顶层 auth_code 空 → 白走一遍。
        is_oauth_callback = (
            "localhost:1455" in url_low or "localhost%3A1455" in url_low
            or "/auth/callback" in url_low
            # Chrome 试图连 localhost:1455 失败 → page.url 返 chrome-error,但
            # 之前的 request URL 已经被 capture handler 抓到了 callback code
            or "chrome-error" in url_low
        )
        if (is_oauth_callback
                or ("chatgpt.com" in url_low and "auth.openai.com" not in url_low
                    and "/auth/" not in url_low)
                or "success" in url_low):
            logger.info(
                "[phone-reg] ✅ phase1.5 OAuth 完成 email=%s url=%s (callback=%s)",
                current_email, url[:100], is_oauth_callback,
            )
            safe_screenshot(page, SCREENSHOT_DIR / "phone_15z_bind_done.png")
            return True, current_email, current_address_id

        # 被重定向到 /log-in — 补一次手机登录(同 phase 2 流程,但不涉及 OAuth)
        has_phone_login_btn = False
        try:
            btns_info = page.evaluate(
                """() => Array.from(document.querySelectorAll('button')).map(b => (b.innerText || '').trim()).filter(t => t)"""
            )
            has_phone_login_btn = any(any(t in b for t in PHONE_LOGIN_TEXTS) for b in btns_info)
        except Exception:
            btns_info = []
        has_phone_input = any(
            i.get("name") == "phoneNumberInput" or i.get("type") == "tel" for i in inputs_info
        )

        # 1) Welcome back / 登录方式选择页 → 点「Continue with phone」
        if has_phone_login_btn and not has_phone_input:
            logger.info("[phone-reg] phase1.5 r%d 点 Continue with phone", round_idx)
            _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=8000)
            _sleep(3)
            last_url = url
            continue

        # 2) 手机号输入页
        if has_phone_input:
            logger.info("[phone-reg] phase1.5 r%d 填手机号 %s", round_idx, phone_e164)
            try:
                _select_country(page, country)
            except Exception:
                pass
            local_number = strip_dial_prefix(phone_e164, country)
            try:
                _fill_phone_input(page, local_number, phone_e164, country)
            except Exception as exc:
                logger.warning("[phone-reg] phase1.5 手机号填写失败(放弃): %s", exc)
                return False, current_email, current_address_id
            _click_submit_button(page)
            _sleep(3)
            wait_cloudflare(page, max_wait_seconds=30)
            last_url = url
            continue

        # 3) 密码页(/log-in/password 或含 password 输入框)— 填密码
        is_pw_page = ("password" in url_low and "add-email" not in url_low) or any(
            i.get("type") == "password" for i in inputs_info
        )
        if is_pw_page:
            logger.info("[phone-reg] phase1.5 r%d 密码页 — 填密码", round_idx)
            try:
                _fill_password_input(page, password)
            except Exception as exc:
                logger.warning("[phone-reg] phase1.5 密码填写失败(放弃): %s", exc)
                return False, current_email, current_address_id
            _sleep(0.5)
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            _sleep(1)
            _click_submit_button(page)
            _sleep(4)
            wait_cloudflare(page, max_wait_seconds=30)
            last_url = url
            continue

        # 4) /add-email — 填邮箱
        if ("add-email" in url_low or "add_email" in url_low) and not email_submitted:
            logger.info("[phone-reg] phase1.5 r%d /add-email — 填 %s", round_idx, current_email)
            try:
                _fill_add_email(page, current_email)
            except Exception as exc:
                logger.warning("[phone-reg] phase1.5 /add-email 填写失败: %s", exc)
                safe_screenshot(page, SCREENSHOT_DIR / "phone_15_add_email_fill_fail.png")
                return False, current_email, current_address_id
            # /add-email 反爬严:用 Playwright 真点击(isTrusted=true),否则后端
            # silent drop 不发邮件(详见 _click_submit_button_real 注释)
            #
            # ★ 诊断:抓 submit 后的网络请求 — 定位 OpenAI 后端实际收到了什么 +
            # 返了什么状态码。inbox 一直 0 封,需要看 API 层证据
            captured_responses: list[dict] = []

            def _on_response(resp):
                try:
                    rurl = resp.url
                    if "openai.com" not in rurl and "chatgpt.com" not in rurl:
                        return
                    method = ""
                    req_headers: dict = {}
                    req_body = ""
                    try:
                        method = resp.request.method
                        req_headers = resp.request.headers or {}
                    except Exception:
                        pass
                    try:
                        req_body = resp.request.post_data or ""
                    except Exception:
                        pass
                    info = {
                        "method": method,
                        "url": rurl[:180],
                        "status": resp.status,
                    }
                    # 关注的请求(可能涉及 email send):抓 request headers + body + response body
                    rurl_low = rurl.lower()
                    is_relevant = (
                        resp.status >= 400
                        or "add-email" in rurl_low
                        or "email" in rurl_low
                        or "verification" in rurl_low
                        or "verify" in rurl_low
                        or "/backend-api" in rurl_low
                        or "/api/auth" in rurl_low
                        or "/api/accounts" in rurl_low
                    )
                    if is_relevant:
                        # 关键 header 筛选 — 排除 cookie / authorization 等冗长字段,留指纹相关
                        interesting_keys = (
                            "user-agent", "accept", "accept-language", "origin", "referer",
                            "sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile",
                            "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
                            "content-type", "x-csrf-token", "x-requested-with",
                            "x-oai-client-locale", "x-oai-mobile-os", "oai-client-version",
                        )
                        info["req_headers"] = {
                            k: req_headers[k] for k in req_headers if k.lower() in interesting_keys
                        }
                        info["req_body"] = (req_body or "")[:300]
                        try:
                            body = resp.text() or ""
                            info["body"] = body[:400]
                        except Exception:
                            info["body"] = "<body read failed>"
                    captured_responses.append(info)
                except Exception:
                    pass

            page.on("response", _on_response)
            try:
                _click_submit_button_real(page)
                _sleep(4)
                wait_cloudflare(page, max_wait_seconds=30)
                _sleep(2)
            finally:
                try:
                    page.remove_listener("response", _on_response)
                except Exception:
                    pass

            email_submitted = True
            # 诊断:dump submit 后的页面状态
            try:
                post_url = (page.url or "")[:160]
                post_body = page.evaluate(
                    "() => (document.body && document.body.innerText || '').slice(0, 600)"
                )
                post_errors = page.evaluate(
                    """() => Array.from(document.querySelectorAll('[role=alert], [aria-live], .error, .alert, [class*=error i], [class*=Error]'))
                        .map(el => (el.innerText || '').trim()).filter(t => t).slice(0, 5)"""
                )
                logger.info(
                    "[phone-reg] [DIAG] phase1.5 submit 后 url=%s errors=%r body[:600]=%r",
                    post_url, post_errors, (post_body or "")[:600],
                )
            except Exception:
                pass

            # 诊断:dump 网络请求 — 看 OpenAI 实际收到了什么 + 返了什么
            try:
                logger.warning(
                    "[phone-reg] [DIAG] phase1.5 submit 后捕获到 %d 个 OpenAI/chatgpt 响应:",
                    len(captured_responses),
                )
                for idx, r in enumerate(captured_responses[:25]):
                    body_preview = r.get("body") or ""
                    req_h = r.get("req_headers") or {}
                    req_b = r.get("req_body") or ""
                    if body_preview or req_h or req_b:
                        logger.warning(
                            "  [%d] %s %d %s",
                            idx, r.get("method") or "?", r.get("status") or 0,
                            r.get("url") or "",
                        )
                        if req_h:
                            logger.warning("      req_headers=%r", req_h)
                        if req_b:
                            logger.warning("      req_body=%r", req_b)
                        if body_preview:
                            logger.warning("      resp_body[:400]=%r", body_preview)
                    else:
                        logger.warning(
                            "  [%d] %s %d %s",
                            idx, r.get("method") or "?", r.get("status") or 0,
                            r.get("url") or "",
                        )
            except Exception:
                pass

            safe_screenshot(page, SCREENSHOT_DIR / "phone_15c_add_email_after_submit.png")
            last_url = url
            continue

        # 5) /email-verification / OTP 页 — 等 cloud-mail OTP (60s 超时直接放弃)
        is_email_otp = (
            "email-verification" in url_low
            or "email-otp" in url_low
            or (email_submitted and any(
                i.get("type") in ("text", "tel", "number") and i.get("name") != "phoneNumberInput"
                for i in inputs_info
            ) and ("code" in (body_text or "").lower()
                   or "verification" in (body_text or "").lower()
                   or "验证码" in (body_text or "")))
        )
        if is_email_otp and not otp_submitted:
            logger.info("[phone-reg] phase1.5 r%d /email-verification — 等 %s OTP %ds",
                        round_idx, current_email, OTP_TIMEOUT_S)
            try:
                _, mail_code = mail_client.wait_for_otp(
                    current_email, after_id=mail_baseline_id, timeout=OTP_TIMEOUT_S,
                )
            except Exception as exc:
                logger.warning("[phone-reg] phase1.5 OTP %ds 超时: %s — 放弃 phase 1.5",
                               OTP_TIMEOUT_S, exc)
                safe_screenshot(page, SCREENSHOT_DIR / "phone_15_otp_timeout.png")
                return False, current_email, current_address_id
            # OTP 拿到,填写
            if not _fill_sms_code_smart(page, mail_code):
                logger.warning("[phone-reg] phase1.5 OTP 填写失败")
                safe_screenshot(page, SCREENSHOT_DIR / "phone_15_otp_fill_fail.png")
                return False, current_email, current_address_id
            _click_submit_button(page)
            _sleep(4)
            wait_cloudflare(page, max_wait_seconds=30)
            _sleep(2)
            otp_submitted = True
            last_url = url
            continue

        # 6) /choose-an-account picker — phase 1 cookies 留下了账号,OAuth 看到自动出 picker
        # JS 找卡片打 marker → Playwright 真点击(isTrusted=true)
        # 之前用 el.click() 是合成事件,React picker UI 不响应,卡 picker 死循环
        if "choose-an-account" in url_low or "account-picker" in url_low or "Welcome back" in (body_text or ""):
            logger.warning(
                "[phone-reg] phase1.5 r%d Account picker — 找卡片真点击",
                round_idx,
            )
            try:
                # Step 1: JS 找含 @ 或 +<digits> 的卡片元素,打 marker(不点击)
                marked_text = page.evaluate(
                    """() => {
                        const skip = ['log in to another', 'use another', 'use a different',
                                      'sign in to another', '另一个账号', '其他账号', '换个账号',
                                      'create account', '创建账号', 'log out', '登出', 'sign up',
                                      'terms of use', 'privacy policy', '使用条款', '隐私政策'];
                        // 清掉上轮可能留下的 marker
                        document.querySelectorAll('[data-autofree-picker]').forEach(
                            el => el.removeAttribute('data-autofree-picker')
                        );
                        const cands = document.querySelectorAll(
                            'button, a, li, div[role="button"], [data-testid]'
                        );
                        for (const el of cands) {
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            const t = (el.innerText || '').trim().toLowerCase();
                            if (!t || t.length > 200) continue;
                            if (skip.some(s => t.includes(s))) continue;
                            if (t.includes('@') || /\\+\\d{1,4}/.test(t)) {
                                el.scrollIntoView({ block: 'center' });
                                el.setAttribute('data-autofree-picker', '1');
                                return t.slice(0, 80);
                            }
                        }
                        return null;
                    }"""
                )
                if marked_text:
                    # Step 2: Playwright 真点击 marker(CDP → isTrusted=true)
                    try:
                        page.locator('[data-autofree-picker="1"]').first.click(timeout=8000)
                        logger.info("[phone-reg] phase1.5 picker 真点击: %r", marked_text)
                        _sleep(4)
                        last_url = url
                        continue
                    except Exception as click_exc:
                        logger.warning(
                            "[phone-reg] phase1.5 picker 真点击失败(%s)— 兜底 dispatchEvent",
                            click_exc,
                        )
                        # 兜底:真点击不行就用合成事件试一下(大概率也不行,但记一笔)
                        page.evaluate(
                            "() => document.querySelector('[data-autofree-picker=\"1\"]')?.click()"
                        )
                        _sleep(3)
                        last_url = url
                        continue
                else:
                    logger.warning("[phone-reg] phase1.5 picker 没找到账号卡片")
                    safe_screenshot(
                        page, SCREENSHOT_DIR / f"phone_15_picker_no_card_r{round_idx:02d}.png",
                    )
            except Exception as exc:
                logger.warning("[phone-reg] phase1.5 picker 处理异常: %s", exc)

        # 7) consent 页面 — 用 body 文本判定(/oauth/authorize? 初始 URL 也含 /authorize,
        # 但内容不是 consent,改用 "Sign in to" / "By continuing" 等 consent 特征文本)
        is_consent = (
            "/consent" in url_low
            or "Sign in to" in (body_text or "")
            or "By continuing, ChatGPT" in (body_text or "")
        )
        if is_consent:
            logger.info("[phone-reg] phase1.5 r%d consent — 真点击 Continue", round_idx)
            # 跟 picker 一样:JS 找按钮打 marker,Playwright 真点击(isTrusted)
            try:
                marked = page.evaluate(
                    """(texts) => {
                        document.querySelectorAll('[data-autofree-consent]').forEach(
                            el => el.removeAttribute('data-autofree-consent')
                        );
                        const nodes = document.querySelectorAll('button, [role="button"], a');
                        for (const b of nodes) {
                            if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                            const t = (b.innerText || b.textContent || '').trim();
                            if (!t) continue;
                            const rect = b.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            // 排除 Cancel
                            const tlow = t.toLowerCase();
                            if (tlow.includes('cancel') || tlow === '取消') continue;
                            if (texts.some(tx => t.includes(tx))) {
                                b.setAttribute('data-autofree-consent', '1');
                                return t;
                            }
                        }
                        return null;
                    }""",
                    list(ALLOW_BUTTON_TEXTS),
                )
                if marked:
                    try:
                        page.locator('[data-autofree-consent="1"]').first.click(timeout=8000)
                        logger.info("[phone-reg] phase1.5 consent 真点击: %r", marked)
                    except Exception as click_exc:
                        logger.warning(
                            "[phone-reg] phase1.5 consent 真点击失败(%s),兜底 dispatchEvent",
                            click_exc,
                        )
                        _click_button_by_text(page, ALLOW_BUTTON_TEXTS, timeout_ms=8000)
                else:
                    logger.warning("[phone-reg] phase1.5 consent 没找到 Allow/Continue 按钮")
                    _click_button_by_text(page, ALLOW_BUTTON_TEXTS, timeout_ms=8000)
            except Exception as exc:
                logger.warning("[phone-reg] phase1.5 consent 处理异常: %s", exc)
                _click_button_by_text(page, ALLOW_BUTTON_TEXTS, timeout_ms=8000)
            _sleep(4)
            last_url = url
            continue

        last_url = url

    logger.warning("[phone-reg] phase1.5 绑邮箱 %d 轮超时,放弃。最后 url=%s",
                   max_rounds, page.url)
    safe_screenshot(page, SCREENSHOT_DIR / "phone_15_timeout.png")
    return False, current_email, current_address_id


# ─── Phase 2: auth.openai.com OAuth 阶段 ─────────────────────────────────────

def _phase2_oauth(
    page: Page,
    auth_url: str,
    sms_provider,
    order,
    country: PhoneCountry,
    phone_e164: str,
    email_for_bind: str,
    mail_client,
    password: str,
    *,
    capture_callback,
) -> bool:
    """auth.openai.com OAuth 流程:用同一 phone 重新登录,自动 add-email,等 callback。

    返回 email_bound:True = /add-email 已触发并绑了邮箱,False = 走 picker
    shortcut 没绑(账号 phone-only,后续 reauth 必须 SMS)。
    callback 是否捕获到 code 由调用方 capture_callback 检测,失败时本函数 raise OAuthFailed。
    """
    logger.info("[phone-reg] === Phase 2 OAuth 开始 ===")
    safe_screenshot(page, SCREENSHOT_DIR / "phone_20_phase2_start.png")

    # cloud-mail baseline:用于 add-email 后的 OTP 不抓到老邮件
    try:
        mail_baseline_id = mail_client.latest_mail_id(email_for_bind)
    except Exception:
        mail_baseline_id = 0
    logger.info("[phone-reg] cloud-mail baseline id=%d for %s", mail_baseline_id, email_for_bind)

    # 关键:清掉 phase1 留下的 chatgpt.com / auth.openai.com / openai.com 域 cookies。
    # 否则 OAuth 启动时 OpenAI 看到已有 session → 出「Welcome back」picker 走 shortcut →
    # 跳过 /add-email → 拿到无效 code → token 交换返 token_exchange_user_error。
    # 清掉后 OAuth 必走完整登录(phone + SMS#2 + /add-email),才能拿到能换 token 的真 code。
    try:
        cleared = 0
        for ck in page.context.cookies():
            dom = (ck.get("domain") or "").lower().lstrip(".")
            if dom.endswith("openai.com") or dom.endswith("chatgpt.com"):
                cleared += 1
        page.context.clear_cookies()
        logger.info("[phone-reg] 已清 OpenAI/ChatGPT 域 cookies(共 %d 条)— 强制 phase2 走完整登录触发 /add-email", cleared)
    except Exception as exc:
        logger.warning("[phone-reg] 清 cookies 失败(继续,但 picker 可能出现): %s", exc)
    # 顺手清掉 localStorage / sessionStorage / IndexedDB / Cache Storage / Service Worker —
    # OpenAI 把 OAuth session 散放在这些地方,只清 cookie 不够。必须在 *.openai.com /
    # chatgpt.com 域上下文里清才有效 — 现在 page 还在 chatgpt.com,先 goto 各 OpenAI 域
    _CLEAR_STORAGE_JS = """
        () => {
            const r = { ls: 0, ss: 0, idb: [], cache: 0, sw: 0 };
            try {
                r.ls = localStorage.length;
                localStorage.clear();
            } catch(e) {}
            try {
                r.ss = sessionStorage.length;
                sessionStorage.clear();
            } catch(e) {}
            const tasks = [];
            // IndexedDB:列出所有 db 并 delete
            try {
                if (indexedDB.databases) {
                    tasks.push(indexedDB.databases().then(dbs => {
                        return Promise.all(dbs.map(db => new Promise(res => {
                            try {
                                const req = indexedDB.deleteDatabase(db.name);
                                req.onsuccess = req.onerror = req.onblocked = () => res(db.name);
                            } catch(e) { res(null); }
                        }))).then(names => { r.idb = names.filter(Boolean); });
                    }));
                }
            } catch(e) {}
            // Cache Storage(Service Worker 缓存)
            try {
                if (typeof caches !== 'undefined' && caches.keys) {
                    tasks.push(caches.keys().then(keys => {
                        r.cache = keys.length;
                        return Promise.all(keys.map(k => caches.delete(k)));
                    }));
                }
            } catch(e) {}
            // Service Worker — 注销所有 reg,避免他们拦截后续请求重新挂回 session
            try {
                if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
                    tasks.push(navigator.serviceWorker.getRegistrations().then(regs => {
                        r.sw = regs.length;
                        return Promise.all(regs.map(reg => reg.unregister()));
                    }));
                }
            } catch(e) {}
            return Promise.all(tasks).then(() => r);
        }
    """
    for origin in ("https://chatgpt.com/", "https://auth.openai.com/"):
        try:
            page.goto(origin, wait_until="domcontentloaded", timeout=15000)
            cleared_info = page.evaluate(_CLEAR_STORAGE_JS)
            logger.info("[phone-reg] 已清 %s 上的存储: %s", origin, cleared_info)
        except Exception as exc:
            logger.warning("[phone-reg] 清 %s 存储失败: %s", origin, exc)

    # 主动访问 /logout — 让 OpenAI 后端 invalidate session,防止 picker
    try:
        page.goto("https://auth.openai.com/logout", wait_until="domcontentloaded", timeout=15000)
        _sleep(2)
        logger.info("[phone-reg] 已主动访问 auth.openai.com/logout")
    except Exception as exc:
        logger.warning("[phone-reg] /logout 访问失败(继续): %s", exc)
    # 再清一次 cookies — /logout 可能写新 cookie
    try:
        page.context.clear_cookies()
        logger.info("[phone-reg] /logout 后再清一次 cookies")
    except Exception:
        pass

    page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
    _sleep(3)
    wait_cloudflare(page, max_wait_seconds=90)
    safe_screenshot(page, SCREENSHOT_DIR / "phone_21_oauth_loaded.png")

    email_bound = False
    last_url = ""

    for round_idx in range(40):
        if is_stop_requested():
            raise BatchStopped("phone reg phase2 收到 stop")

        _sleep(3)

        # callback 抓到了?
        if capture_callback():
            if email_bound:
                logger.info("[phone-reg] ✅ Phase 2 callback 已捕获(email 已绑定 %s)",
                            email_for_bind)
            else:
                logger.warning(
                    "[phone-reg] ⚠️ Phase 2 callback 已捕获,但 /add-email 未触发 — "
                    "账号 phone=%s 未绑定 email,以后 reauth 必须用 SMS(花钱)。"
                    "如要补绑请手动登录 chatgpt.com/account/settings 用此手机号登录后加 email %s",
                    phone_e164, email_for_bind,
                )
            return email_bound

        try:
            url = (page.url or "")
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
            logger.debug("[phone-reg] phase2 r%d eval 失败", round_idx)
            last_url = ""
            continue

        # chrome-error(localhost callback 拒连)→ 等 capture_callback 命中
        if "chrome-error" in url_low:
            _sleep(2)
            if capture_callback():
                return True

        # 账号已废检测
        try:
            assert_account_alive(page, f"phone_oauth_r{round_idx}")
        except Exception:
            raise

        # OAuth 错误页
        err_marker = _detect_oauth_error(page)
        if err_marker:
            logger.warning("[phone-reg] phase2 r%d OAuth 错误页 marker=%s", round_idx, err_marker)
            if _click_button_by_text(page, RETRY_BUTTON_TEXTS, timeout_ms=3000):
                _sleep(3)
                wait_cloudflare(page, max_wait_seconds=30)
                last_url = ""
                continue

        if url == last_url:
            continue
        # 诊断:每次 URL 变化都 dump 一份 inputs / buttons / body 片段
        input_types = [i.get("type") for i in inputs_info if i.get("type")]
        logger.info(
            "[phone-reg] [DIAG] phase2 r%d url=%s inputs=%s btns=%s body[:200]=%r",
            round_idx, url[:120], input_types[:8], btns_info[:10], (body_text or "")[:200],
        )

        # 1) 登录/注册选择页 — 找「继续使用手机登录」入口
        has_phone_login_btn = any(any(t in b for t in PHONE_LOGIN_TEXTS) for b in btns_info)
        has_phone_input = any(
            i.get("name") == "phoneNumberInput" or i.get("type") == "tel" for i in inputs_info
        )
        if has_phone_login_btn and not has_phone_input:
            logger.info("[phone-reg] phase2 r%d 点「继续使用手机登录」", round_idx)
            _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=8000)
            _sleep(3)
            last_url = url
            continue

        # 2) 手机号输入页(prompt=login 强制走 /log-in,这里需要再填一次同 phone)
        if has_phone_input:
            logger.info("[phone-reg] phase2 r%d 手机号输入页", round_idx)
            try:
                _select_country(page, country)
            except Exception:
                pass
            local_number = strip_dial_prefix(phone_e164, country)
            try:
                _fill_phone_input(page, local_number, phone_e164, country)
            except Exception as exc:
                raise OAuthFailed(f"OAuth 手机号填写失败: {exc}") from exc
            _click_submit_button(page)
            _sleep(3)
            wait_cloudflare(page, max_wait_seconds=30)
            _sleep(3)
            safe_screenshot(page, SCREENSHOT_DIR / "phone_22_oauth_phone_submit.png")
            last_url = url
            continue

        # 3) SMS 验证码页(/contact-verification 或 code 输入框)
        is_code_page = "contact-verification" in url_low or "phone-verification" in url_low or any(
            (i.get("type") in ("text", "tel", "number") and i.get("name") != "phoneNumberInput"
             and ("code" in (i.get("name") or "").lower()
                  or "code" in (i.get("placeholder") or "").lower()
                  or any(s in (i.get("placeholder") or "").lower()
                         for s in ("verification", "验证")))) for i in inputs_info
        )
        if is_code_page:
            logger.info("[phone-reg] phase2 r%d 等 SMS#2 order=%d", round_idx, order.id)
            try:
                code = sms_provider.wait_for_otp(
                    order_id=order.id, timeout=SMS_WAIT_SECONDS,
                    should_stop=is_stop_requested,
                )
            except sms_mod.SmsAborted:
                raise BatchStopped("phone reg phase2 等 SMS#2 收到 stop")
            except sms_mod.SmsTimeout as exc:
                raise OAuthFailed(f"OAuth SMS#2 超时 ({SMS_WAIT_SECONDS}s),同一订单第 2 条 SMS 未到") from exc

            if not _fill_sms_code_smart(page, code):
                safe_screenshot(page, SCREENSHOT_DIR / "phone_23_oauth_code_fill_failed.png")
                raise OAuthFailed("OAuth SMS code 填写失败")
            _click_submit_button(page)
            _sleep(3)
            wait_cloudflare(page, max_wait_seconds=30)
            _sleep(2)
            safe_screenshot(page, SCREENSHOT_DIR / "phone_24_oauth_after_sms.png")
            last_url = url
            continue

        # 4) /add-email 邮箱绑定 — JS focus + type 避开 React Aria 浮动 label 拦截
        if "add-email" in url_low or "add_email" in url_low:
            logger.info("[phone-reg] phase2 r%d /add-email,绑定 %s", round_idx, email_for_bind)
            try:
                # 等 input 渲染
                deadline = time.monotonic() + 5
                ei_sel = ('input[type="email"], input[name="email"], '
                          'input[name="username"], input[name="identifier"]')
                ready = False
                while time.monotonic() < deadline:
                    if page.evaluate(f"() => !!document.querySelector('{ei_sel}')"):
                        ready = True
                        break
                    _sleep(0.3)
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
                    logger.warning("[phone-reg] /add-email focus 失败 — force click 兜底")
                    page.locator(ei_sel).first.click(force=True, timeout=3000)
                _sleep(0.2)
                try:
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Delete")
                except Exception:
                    pass
                page.keyboard.type(email_for_bind, delay=30)
            except OAuthFailed:
                safe_screenshot(page, SCREENSHOT_DIR / "phone_25_add_email_no_input.png")
                raise
            except Exception as exc:
                safe_screenshot(page, SCREENSHOT_DIR / "phone_25_add_email_no_input.png")
                raise OAuthFailed(f"/add-email 填写失败: {exc}") from exc
            _sleep(0.5)
            # /add-email 反爬严:用真点击(isTrusted=true)— 跟 phase 1.5 同因
            _click_submit_button_real(page)
            _sleep(4)
            wait_cloudflare(page, max_wait_seconds=30)
            _sleep(2)
            email_bound = True
            safe_screenshot(page, SCREENSHOT_DIR / "phone_26_add_email_submit.png")
            last_url = url
            continue

        # 5) /email-verification 邮件 OTP
        is_email_otp = ("email-verification" in url_low or
                        (email_bound and any(
                            i.get("type") in ("text", "tel", "number") and i.get("name") != "phoneNumberInput"
                            for i in inputs_info
                        ) and ("code" in body_text.lower() or "verification" in body_text.lower()
                               or "验证码" in body_text)))
        if is_email_otp:
            logger.info("[phone-reg] phase2 r%d /email-verification,等 cloud-mail OTP", round_idx)
            try:
                _, mail_code = mail_client.wait_for_otp(
                    email_for_bind, after_id=mail_baseline_id, timeout=EMAIL_POLL_TIMEOUT,
                )
            except Exception as exc:
                raise OAuthFailed(f"cloud-mail OTP 超时: {exc}") from exc
            if not _fill_sms_code_smart(page, mail_code):
                safe_screenshot(page, SCREENSHOT_DIR / "phone_27_email_code_fill_failed.png")
                raise OAuthFailed("/email-verification code 填写失败")
            _click_submit_button(page)
            _sleep(4)
            wait_cloudflare(page, max_wait_seconds=30)
            _sleep(2)
            safe_screenshot(page, SCREENSHOT_DIR / "phone_28_email_verified.png")
            last_url = url
            continue

        # 6) about-you(phase2 也可能出现 — 部分号要求二次确认)
        if "about-you" in url_low or "about_you" in url_low:
            logger.info("[phone-reg] phase2 r%d about-you", round_idx)
            _fill_about_you(page, random_full_name(), random_birthday())
            _sleep(5)
            wait_cloudflare(page, max_wait_seconds=30)
            _sleep(2)
            last_url = url
            continue

        # 7) 密码页 — Phase 1 设过密码,这里用同一个密码登录(对照 JS 参考 browserService.js:1607-1654)
        is_pw_page = "password" in url_low or any(i.get("type") == "password" for i in inputs_info)
        if is_pw_page:
            # 诊断:dump 页面所有 a / [role=link] / 副按钮 — 看 OpenAI 是否给了「用其他方式登录」「忘记密码」入口
            try:
                links_info = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a, [role="link"]')).map(a => ({
                        text: (a.innerText || a.textContent || '').trim().slice(0, 80),
                        href: a.getAttribute('href') || '',
                    })).filter(x => x.text)"""
                )
            except Exception:
                links_info = []
            logger.info(
                "[phone-reg] [DIAG] phase2 r%d 密码页全量 dump | url=%s | inputs=%s | btns=%s | links=%s | body[:300]=%r",
                round_idx, url, input_types[:10], btns_info[:15], links_info[:15],
                (body_text or "")[:300],
            )
            if not password:
                safe_screenshot(page, SCREENSHOT_DIR / "phone_29_password_empty.png")
                raise OAuthFailed("OAuth 密码页出现但 password 参数为空 — Phase 1 未设密码?")
            logger.info("[phone-reg] phase2 r%d 密码页,填密码 url=%s", round_idx, url[:80])
            try:
                _fill_password_input(page, password)
            except Exception as exc:
                safe_screenshot(page, SCREENSHOT_DIR / "phone_29_password_fill_failed.png")
                raise OAuthFailed(f"OAuth 密码填写失败: {exc}") from exc
            _sleep(0.5)
            # 优先回车提交(OpenAI 登录页支持),再补一次按钮点击
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            _sleep(1)
            _click_submit_button(page)
            _sleep(4)
            wait_cloudflare(page, max_wait_seconds=60)
            _sleep(2)
            safe_screenshot(page, SCREENSHOT_DIR / "phone_29b_after_password.png")
            # 密码错检测:仍在密码页 → 抛错
            try:
                still_pw = page.locator(PASSWORD_INPUT_SELECTOR).first.is_visible(timeout=2000)
            except Exception:
                still_pw = False
            if still_pw and "password" in (page.url or "").lower():
                # 检查页面是否有「密码错」错误提示
                try:
                    body_low = (page.locator("body").inner_text(timeout=1500) or "").lower()
                except Exception:
                    body_low = ""
                err_markers = ("incorrect", "wrong password", "密码错", "密码不", "invalid password")
                if any(m in body_low for m in err_markers):
                    raise OAuthFailed(
                        "OAuth 密码错误 — Phase 1 设的密码和 Phase 2 不一致(可能 chatgpt.com 注册流程没设密码就直跳了)"
                    )
                logger.warning("[phone-reg] phase2 r%d 密码提交后仍在密码页(未见明确错误),继续重试", round_idx)
            last_url = url
            continue

        # 7.5) 「Welcome back / Choose an account」picker
        #
        # 修复历史:
        # - 早期点已有账号卡片 → 报「跳过 /add-email」(误判,可能是 phase1 没绑 password 才走快路径)
        # - 中期点「Log in to another account」→ 启动了**全新 OAuth 实例**,新 state
        #   ≠ CPA 的 state,回填 CPA 报 404 unknown state(已 HAR 实测验证)
        # - 现版:**点已有账号卡片**继续当前 OAuth(state 保留)。OpenAI 看 phone-only
        #   账号会自动跳 /add-email。即使没跳,也至少 state 匹配。
        #
        # 上策仍是用 fresh context(register_phone_and_fetch_bundle 已实施)避免 picker,
        # 这里是 picker 万一出现的 fallback。
        try:
            body_text = page.inner_text("body", timeout=1500)[:2000]
        except Exception:
            body_text = ""
        if ("Welcome back" in body_text or "选择账号" in body_text
                or "Choose an account" in body_text):
            logger.warning(
                "[phone-reg] phase2 r%d Account picker 出现 — fresh context 没挡住,"
                "点已有账号卡片继续当前 OAuth(保留 state,等 /add-email)",
                round_idx,
            )
            try:
                clicked = page.evaluate(
                    """() => {
                        const skip = ['log in to another', 'use another', 'use a different',
                                      'sign in to another', '另一个账号', '其他账号', '换个账号',
                                      'create account', '创建账号', 'log out', '登出', 'sign up',
                                      'terms of use', 'privacy policy', '使用条款', '隐私政策'];
                        const cands = document.querySelectorAll(
                            'button, a, li, div[role="button"], [data-testid]'
                        );
                        for (const el of cands) {
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            const t = (el.innerText || '').trim().toLowerCase();
                            if (!t || t.length > 200) continue;
                            if (skip.some(s => t.includes(s))) continue;
                            // 账号卡片特征:含邮箱 @ 或国际电话 +<digits>
                            if (t.includes('@') || /\\+\\d{1,4}/.test(t)) {
                                el.scrollIntoView({ block: 'center' });
                                el.click();
                                return t.slice(0, 80);
                            }
                        }
                        return null;
                    }"""
                )
                if clicked:
                    logger.info("[phone-reg] picker 点中账号卡片: %r — 继续当前 OAuth,等 /add-email",
                                clicked)
                    _sleep(3)
                    last_url = url
                    continue
                logger.error(
                    "[phone-reg] picker 没找到账号卡片(可能 UI 变了)— OAuth 流程可能卡住"
                )
            except Exception as exc:
                logger.warning("[phone-reg] account picker 处理异常: %s", exc)

        # 8) consent 页面:点 Allow/Continue
        if "/consent" in url_low or "/authorize" in url_low:
            logger.info("[phone-reg] phase2 r%d consent 页", round_idx)
            if _click_button_by_text(page, ALLOW_BUTTON_TEXTS, timeout_ms=8000):
                _sleep(4)
                last_url = url
                continue

        # 9) 兜底:页面变化但都没命中 → 等
        last_url = url

    safe_screenshot(page, SCREENSHOT_DIR / "phone_30_phase2_timeout.png")
    raise OAuthFailed(f"Phase 2 OAuth 40 轮超时,最后 url={page.url}")


# ─── 顶层入口 ───────────────────────────────────────────────────────────────

def register_phone_and_fetch_bundle(
    *,
    email: str,
    address_id: int | None = None,
    password: str,
    mail_client,
) -> dict:
    """手机号注册一体化入口:浏览器开 → 注册 → OAuth → 拿 codex bundle → 关浏览器。

    参数:
      email       — 用于 OAuth /add-email 绑定的 cloud-mail 邮箱(预先 create_email 出来)
      address_id  — 上述 email 的 cloud-mail id(签名保留,phase 1.5 不再换邮箱)
      password    — 设给账号的密码(可能 phase2 不用,但 phase1 必填)
      mail_client — cloud-mail 客户端(收 add-email 后的 OTP)

    返回 bundle(标准格式,跟 email-reg 一致):
      {access_token, refresh_token, id_token, account_id, email, plan_type,
       expires_at, phone_verified=True, phone, email_bound, via_cpa=False}

    若 phase 1.5 换过邮箱:
      bundle["email"] = 实际绑定的新邮箱
      bundle["bound_address_id"] = 新邮箱 cloud-mail id(batch.py 用它更新本地状态)

    OAuth 实现:
      自生 PKCE verifier/challenge/state → 浏览器跑 OAuth → 自己用 verifier+code
      换 token → 跟 email-reg 走同一条 write_auth_json + push_auth_file 路径。
      历史上有走「找 CPA 取 auth_url + 回填 CPA」的路径,因 OpenAI picker
      shortcut 改 state 导致 CPA 404,已彻底删除。

    SMS 配置(provider/country/operator/service)从 DB Setting 表读 — 跟 oauth.py
    走 phone gate 时是同一套 active provider。
    """
    if not email:
        raise ValueError("email 不能为空(用于 OAuth /add-email 绑定)")

    sms_cfg = get_sms_config()
    try:
        sms_provider = sms_mod.get_active_provider(sms_cfg)
    except sms_mod.SmsConfigMissing as exc:
        raise RegisterFailed(f"SMS provider 未配置: {exc}") from exc

    country = from_sms_slug(sms_cfg.get("country", ""))

    # OAuth:永远自生 PKCE(verifier+challenge+state)。token 自己换,
    # auth.json 走跟 email-reg 一样的 /v0/management/auth-files 文件上传路径。
    # 不再走 CPA 的 /codex-auth-url + /oauth-callback(state 校验路径)— 那条路
    # 在 picker shortcut 出现时必 404,且 CPA 那边 state TTL 短,phase2 拉得久也会过期。
    code_verifier, code_challenge = _pkce()
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(code_challenge, state)
    logger.info("[phone-reg] === AutoFree PKCE === state=%s redirect=%s",
                state[:8], CODEX_REDIRECT_URI)

    logger.info(
        "[phone-reg] 启动 email=%s provider=%s sms_country=%s phone_country=ISO=%s dial=+%s",
        email, sms_provider.PROVIDER_NAME, sms_cfg.get("country"),
        country.iso_code, country.dial_code,
    )

    # 身份资料
    full_name = random_full_name()
    birth = random_birthday()

    # 浏览器选项(proxy session id 用 email prefix 便于 IPRoyal 追踪)
    proxy_session_id = make_proxy_session_id(prefix=email.split("@", 1)[0])
    proxy_opts = get_proxy_options(session_id=proxy_session_id)
    launch_kwargs = get_launch_options()
    if proxy_opts:
        launch_kwargs["proxy"] = proxy_opts
        logger.info("[phone-reg] 使用代理 session=%s", proxy_session_id)

    auth_code: list[str | None] = [None]
    order = None
    phone_e164 = ""
    finished_committed = False

    with email_screenshot_scope(email) as ss_dir, sync_playwright() as p:
        logger.info("[phone-reg] 截图目录: %s", ss_dir)
        browser = p.chromium.launch(**launch_kwargs)
        # Phase 1 用 context1;Phase 2 用全新 context2,跟 phase1 完全隔离,
        # 避免 OpenAI 通过 cookie/storage/fingerprint 复用 phase1 的 session
        # 导致 picker shortcut + state 重置
        context = browser.new_context(**get_context_options())
        page = context.new_page()

        def _try_extract_code(u: str, source: str) -> bool:
            if not u or auth_code[0]:
                return False
            ul = u.lower()
            if "/auth/callback" not in ul:
                return False
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            c = qs.get("code", [None])[0]
            if c:
                auth_code[0] = c
                logger.info("[phone-reg] 捕获 auth_code (%s) url=%s", source, u[:120])
                return True
            return False

        # 引用容器,phase2 重建 page 后能更新内部指针
        page_ref: list[Any] = [page]

        def _capture() -> bool:
            try:
                _try_extract_code(page_ref[0].url or "", "live_url")
            except Exception:
                pass
            return auth_code[0] is not None

        def _attach_capture_handlers(target_page) -> None:
            target_page.on("request", lambda req: _try_extract_code(req.url, "request"))
            target_page.on("requestfailed", lambda req: _try_extract_code(req.url, "requestfailed"))
            target_page.on("response", lambda res: _try_extract_code(res.url, "response"))
            target_page.on("framenavigated", lambda f: _try_extract_code(f.url, "framenav"))

        _attach_capture_handlers(page)

        try:
            # Phase 1: chatgpt.com 手机号注册
            order, phone_e164 = _phase1_signup(
                page, sms_provider, sms_cfg, country, password, full_name, birth,
            )

            # Phase 1.5: 走完整 OAuth(同 phase 2 的 auth_url) — 绑邮箱 + 拿 callback code
            #
            # 跟 phase 2 共用顶层的 code_verifier — 这样 phase 1.5 拿到的 callback code
            # 用顶层 verifier 直接换 token 就能成功,**不需要再跑 phase 2**(跑了反而
            # 因为 state 还在 OpenAI 后端同步中会触发 token_exchange_user_error)。
            phase15_ok, bound_email, bound_address_id = _phase15_bind_email(
                page,
                email_for_bind=email,
                address_id_for_bind=address_id,
                mail_client=mail_client,
                password=password,
                phone_e164=phone_e164,
                country=country,
                auth_url=auth_url,
            )
            if phase15_ok:
                if bound_email != email:
                    logger.info(
                        "[phone-reg] ✅ Phase 1.5 邮箱已绑定(换过邮箱 %s -> %s)",
                        email, bound_email,
                    )
                    email = bound_email
                else:
                    logger.info("[phone-reg] ✅ Phase 1.5 邮箱已绑定")
            else:
                logger.warning(
                    "[phone-reg] ⚠️ Phase 1.5 失败 — fallback phase 2 兜底"
                )
                # phase 1.5 内部可能换过邮箱 — 用最后那个继续
                if bound_email and bound_email != email:
                    email = bound_email

            # ─── phase 1.5 成功 = 邮箱已绑 → 关 phase 1 browser → sleep → 走 resume 路径 ───
            #
            # 用户洞察:点 "继续认证" 时能成功换 token,因为账号 state 已稳定
            # (邮箱"早已绑"而不是"刚绑")。直接在 phase 1 context 换 token 会撞
            # token_exchange_user_error,因为账号 state 还在传播(/add-email +
            # /email-verification 让账号变 "in-flux")。
            #
            # 模拟人工 "继续认证":
            #   1. 关 phase 1 browser(释放 in-flux session)
            #   2. finish_order(SMS 已用,确认扣费)
            #   3. sleep 60s 让 OpenAI 后端把账号 state 同步好
            #   4. 调 fetch_personal_bundle — 这是 email-reg / "继续认证" 用的
            #      working 路径(email + password 登录 OAuth → 跳过 /add-email →
            #      换 token → ✅)
            if phase15_ok:
                logger.info(
                    "[phone-reg] ✅ Phase 1.5 绑邮箱完成 — 关 phase 1 browser 切 resume 路径"
                )
                # 关 phase 1 browser(连 context 一起释放)
                try:
                    context.close()
                except Exception as exc:
                    logger.debug("[phone-reg] 关 phase1 context: %s", exc)
                try:
                    browser.close()
                except Exception as exc:
                    logger.debug("[phone-reg] 关 phase1 browser: %s", exc)

                # finish_order(SMS#1 已用,确认扣费 — phase 1 注册成功就该扣)
                try:
                    sms_provider.finish_order(order.id)
                    finished_committed = True
                    logger.info("[phone-reg] SMS order=%d finish_order 提交", order.id)
                except Exception as exc:
                    logger.warning("[phone-reg] finish_order 失败(忽略): %s", exc)

                # 等 OpenAI 后端同步账号 state(邮箱绑定传播到 identity / oauth /
                # token 几个 service)— 用户手动等待 + 切窗口的自然延迟一般够,
                # 60s 安全余量
                logger.info("[phone-reg] sleep 60s 让 OpenAI 后端同步账号 state...")
                time.sleep(60)

                # 走 resume 路径(email + password 登录 OAuth)— 这是 working 的
                # 路径,跟点"继续认证"完全等价
                logger.info(
                    "[phone-reg] === Phase 2 改用 resume 路径(email login OAuth)=== email=%s",
                    email,
                )
                bundle = fetch_personal_bundle(
                    email=email, password=password,
                    mail_client=mail_client, session_token=None,
                )
                bundle["phone_verified"] = True
                bundle["phone"] = phone_e164
                bundle["email_bound"] = True
                bundle["via_cpa"] = False
                bundle["bound_address_id"] = bound_address_id
                logger.info(
                    "[phone-reg] ✅ 全流程成功(phase 1.5 + resume path)account_id=%s",
                    bundle.get("account_id") or "",
                )
                return bundle

            # phase 1.5 失败(邮箱没绑成功)→ 走旧 phase 2 兜底
            logger.info("[phone-reg] phase 1.5 失败,fallback phase 2 兜底")

            # ─── Phase 1 → Phase 2 切换:重建 browser context ───
            # Phase1 注册 chatgpt.com 留下了 cookies / localStorage / IndexedDB / Service
            # Worker / WebGL fingerprint cache 等大量 session 痕迹。即使清掉,OpenAI 后端
            # 可能通过 IP + 浏览器指纹 仍能识别 → 出 picker shortcut → state 被重置 →
            # CPA 回填 404。彻底解决:关闭 phase1 的 context,开全新的 context2,跟
            # phase1 完全隔离,等同于"换浏览器"。
            try:
                context.close()
                logger.info("[phone-reg] phase1 context 已关闭,phase2 用全新 context")
            except Exception as exc:
                logger.warning("[phone-reg] 关 phase1 context 失败(继续): %s", exc)
            context = browser.new_context(**get_context_options())
            page = context.new_page()
            page_ref[0] = page
            _attach_capture_handlers(page)

            # Phase 2: auth.openai.com OAuth — 用全新 context
            email_bound = _phase2_oauth(
                page, auth_url, sms_provider, order, country, phone_e164,
                email_for_bind=email, mail_client=mail_client,
                password=password,
                capture_callback=_capture,
            )

            # callback 等满 30s 兜底
            if not auth_code[0]:
                logger.info("[phone-reg] consent 后等 callback 最多 30s")
                deadline = time.time() + 30
                while time.time() < deadline and not auth_code[0]:
                    _try_extract_code(page.url or "", "tail_url_poll")
                    time.sleep(1)

            if not auth_code[0]:
                safe_screenshot(page, SCREENSHOT_DIR / "phone_31_no_auth_code.png")
                raise OAuthFailed("Phase 2 完成但未捕获到 auth_code")

            # 给 OpenAI 后端时间 finalize auth_code
            time.sleep(1.5)

            # 自己换 token(verifier 在我们手里,跟 CPA state 无关)
            # token 交换走跟浏览器同一个代理 — OpenAI 校验 IP 一致性
            bundle = _exchange_code(
                auth_code[0], code_verifier, fallback_email=email,
                proxies=_proxy_opts_to_requests(proxy_opts),
            )
            bundle["phone_verified"] = True
            bundle["phone"] = phone_e164
            bundle["email_bound"] = bool(email_bound)
            bundle["via_cpa"] = False
            # phase 1.5 若换过邮箱,把新 address_id 带给 batch.py,让它更新本地状态
            # (老 address_id 已在 phase 1.5 内被删,batch 拿了新的才能 reauth)
            bundle["bound_address_id"] = bound_address_id

            # 提交订单(确认扣费)
            try:
                sms_provider.finish_order(order.id)
                finished_committed = True
                logger.info("[phone-reg] ✅ 全流程成功,sms order=%d finish_order 提交",
                            order.id)
            except Exception as exc:
                logger.warning("[phone-reg] finish_order 失败(token 已拿到,不影响主流程): %s", exc)

            return bundle

        except (RegisterBlocked, RegisterFailed, OAuthFailed) as exc:
            # 已知错误 — 退款(SMS 没成功消费就退,成功消费但 OAuth 失败就 cancel)
            # 不论成败,把 phone_e164 / phase1.5 换过的邮箱挂到异常上,让 batch.py 写 pending
            try:
                exc.phone_e164 = phone_e164  # type: ignore[attr-defined]
                # phase 1.5 失败也可能换过邮箱(老的已删) — 让 batch 用新的写 pending
                if "email" in locals():
                    exc.bound_email = email  # type: ignore[attr-defined]
                if "bound_address_id" in locals():
                    exc.bound_address_id = bound_address_id  # type: ignore[attr-defined]
                # SMS 已 finish_order(phase 1.5 path 提前 commit)→ 钱已付,
                # 后续 fetch_personal_bundle 任何失败都算"已付费 oauth_failed"
                if finished_committed:
                    exc.phone_paid_via_sms = True  # type: ignore[attr-defined]
            except Exception:
                pass
            if order is not None and not finished_committed:
                if isinstance(exc, OAuthFailed):
                    # Phase 2 失败:SMS#1 已消费(Phase 1 用过) → cancel 试图退,但
                    # 2 分钟外通常已不退款 → 视为「已付费」,batch.py 据此把 pending 标 💰
                    _safe_refund(sms_provider, order, "cancel")
                    try:
                        exc.phone_paid_via_sms = True  # type: ignore[attr-defined]
                    except Exception:
                        pass
                else:
                    # Phase 1 失败:SMS 未必消费过 → ban(全退 + 不再分同号),不标 💰
                    _safe_refund(sms_provider, order, "ban")
            raise
        except BatchStopped:
            if order is not None and not finished_committed:
                _safe_refund(sms_provider, order, "cancel")
            raise
        except sms_mod.SmsConfigMissing as exc:
            # 配置类错误(api_key 无效 / max_price 低于最低价 / 国家不支持等)
            # — 没有 order 可退,直接转 RegisterFailed,batch 层只 log warning 不打 traceback
            logger.warning("[phone-reg] SMS 配置错误: %s", exc)
            raise RegisterFailed(f"SMS 配置错误: {exc}") from exc
        except Exception as exc:
            logger.exception("[phone-reg] 未预期异常")
            if order is not None and not finished_committed:
                _safe_refund(sms_provider, order, "ban")
            raise OAuthFailed(f"phone-reg 未预期异常: {exc}") from exc
        finally:
            try:
                browser.close()
            except Exception:
                pass
