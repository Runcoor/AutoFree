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
    assert_account_alive,
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

    # 已经显示正确国家? — 严格判定:只看触发器(visible + aria-haspopup=listbox 或
    # select 的当前 selectedIndex),不扫所有 button(避免误判隐藏 listbox 选项)
    already = page.evaluate(
        """(args) => {
            const { iso, dial } = args;
            // 优先看 React Aria 触发器按钮
            for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
                const r = b.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                const t = (b.innerText || '').trim();
                // 必须 包含 +{dial}(完整 token) 或 ({dial})
                const re = new RegExp(`(^|[^0-9])\\\\+${dial}([^0-9]|$)`);
                if (re.test(t) || t.includes(`(${dial})`)) return `trigger:${t}`;
            }
            // 退路:原生 select 的当前选中项
            const sel = document.querySelector('select');
            if (sel) {
                const opt = sel.options[sel.selectedIndex];
                if (!opt) return null;
                if (opt.value === iso) return `select:${opt.text}`;
                const re = new RegExp(`(^|[^0-9])\\\\+${dial}([^0-9]|$)`);
                if (re.test(opt.text) || opt.text.includes(`(${dial})`)) return `select:${opt.text}`;
            }
            return null;
        }""",
        {"iso": iso, "dial": dial},
    )
    if already:
        logger.info("[phone-reg] 国家已是 %s", already)
        return True

    # 检测 UI 类型
    ui_type = page.evaluate(
        """() => {
            const hasBtn = Array.from(document.querySelectorAll('button')).some(
                b => b.getAttribute('aria-haspopup') === 'listbox' && /\\+\\d/.test(b.innerText || '')
            );
            const hasSelect = !!document.querySelector('select');
            if (hasBtn) return 'react-aria';
            if (hasSelect) return 'native';
            return 'unknown';
        }"""
    )
    logger.info("[phone-reg] 国家选择器 UI=%s 目标 iso=%s dial=%s", ui_type, iso, dial)

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
            # 校验:再读 trigger 显示的 dial 是否对
            actual_dial = page.evaluate(
                """() => {
                    for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
                        const r = b.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const m = (b.innerText || '').match(/\\+(\\d+)/);
                        if (m) return m[1];
                    }
                    const sel = document.querySelector('select');
                    if (sel && sel.options[sel.selectedIndex]) {
                        const m = sel.options[sel.selectedIndex].text.match(/\\+(\\d+)/);
                        if (m) return m[1];
                    }
                    return '';
                }"""
            )
            if str(actual_dial) == dial:
                logger.info("[phone-reg] 国家校验通过 dial=+%s", actual_dial)
                return True
            logger.warning("[phone-reg] 国家校验失败:期望 +%s 实际 +%s — 用完整号兜底",
                           dial, actual_dial or "?")
            return False

    logger.warning("[phone-reg] 国家选择全部方法失败,流程将用完整号码兜底")
    return False


# ─── SMS 订单管理 ───────────────────────────────────────────────────────────

def _buy_phone_order(sms_provider, sms_cfg: dict, attempt_idx: int) -> Any:
    """买 SMS 号 — 任何失败/库存空抛 SmsBuyFailed,上层重试。"""
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
    return sms_provider.buy_activation(
        country=country, operator=operator, product=service, max_price=max_price,
    )


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
                # 老版 landing — 点免费注册再点手机登录
                if _click_button_by_text(page, SIGNUP_BUTTON_TEXTS, timeout_ms=5000):
                    _sleep(3)
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
    """填手机号:先看页面国家显示是否对,对就填本地号,不对就填完整号(不带 +)。"""
    inp = page.locator(PHONE_INPUT_SELECTOR).first
    inp.wait_for(state="visible", timeout=15000)
    inp.click(click_count=3)

    # 只读触发器(可见 + aria-haspopup=listbox / 原生 select 的 selectedOption),
    # 避免扫到隐藏 listbox 选项里的其他 +XX
    current_dial = page.evaluate(
        """() => {
            for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
                const r = b.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                const m = (b.innerText || '').match(/\\+(\\d+)/);
                if (m) return m[1];
            }
            const sel = document.querySelector('select');
            if (sel && sel.options[sel.selectedIndex]) {
                const m = sel.options[sel.selectedIndex].text.match(/\\+(\\d+)/);
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

    inp.fill("", timeout=3000)
    _sleep(0.2)
    page.keyboard.type(value, delay=50)
    _sleep(0.5)


def _fill_password_input(page: Page, password: str) -> None:
    """填密码 — clear + type,React 兼容。"""
    pi = page.locator(PASSWORD_INPUT_SELECTOR).first
    pi.wait_for(state="visible", timeout=15000)
    pi.click(click_count=3)
    _sleep(0.15)
    try:
        pi.press("ControlOrMeta+a", timeout=500)
        pi.press("Backspace", timeout=500)
    except Exception:
        pass
    page.keyboard.type(password, delay=30)
    _sleep(0.3)


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
                        for i, ch in enumerate(code):
                            visible_cells[i].fill(ch, timeout=2000)
                            time.sleep(0.05)
                        return True
                    except Exception as exc:
                        logger.warning("[phone-reg] SMS code 6-cell 失败,回退单 input: %s", exc)
                        break
        except Exception:
            continue

    # 单 input 模式
    for sel in CODE_INPUT_SELECTORS:
        try:
            el = page.locator(sel).first
            if not el.is_visible(timeout=1500):
                continue
            try:
                el.fill(code, timeout=3000)
                logger.info("[phone-reg] SMS code 单 input 模式 sel=%s", sel)
                return True
            except Exception:
                el.click(timeout=2000)
                page.keyboard.type(code, delay=50)
                logger.info("[phone-reg] SMS code keyboard.type 模式 sel=%s", sel)
                return True
        except Exception:
            continue
    return False


def _fill_about_you(page: Page, full_name: str, birth: dict[str, str]) -> None:
    """填 about-you(姓名 + 年龄/生日)— 复刻 register.py 已有逻辑。"""
    _sleep(2)

    # 1) 姓名
    try:
        name_input = page.locator(
            'input[name="name"], input[autocomplete="name"], input[placeholder*="name" i], '
            'input[placeholder*="姓名"]'
        ).first
        if name_input.is_visible(timeout=3000):
            name_input.click(click_count=3)
            page.keyboard.type(full_name, delay=30)
            logger.info("[phone-reg] about-you 姓名: %s", full_name)
    except Exception as exc:
        logger.debug("[phone-reg] 姓名填写失败: %s", exc)

    # 2) 年龄(新版)或生日 3 段(旧版 spinbutton)
    age_str = str(int(time.strftime("%Y")) - int(birth["year"]))
    try:
        age_input = page.locator('input[name="age"]').first
        if age_input.is_visible(timeout=1500):
            age_input.click(click_count=3)
            page.keyboard.press("Backspace")
            page.keyboard.type(age_str, delay=50)
            page.evaluate(
                """(v) => {
                    const inp = document.querySelector('input[name="age"]');
                    if (!inp) return;
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(inp, v);
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                age_str,
            )
            logger.info("[phone-reg] about-you 年龄: %s", age_str)
        else:
            # 旧版 spinbutton — 按 aria-label 识别(年/月/日 或 year/month/day)
            spinbuttons = page.locator('[role="spinbutton"]')
            try:
                cnt = spinbuttons.count()
            except Exception:
                cnt = 0
            if cnt >= 3:
                logger.info("[phone-reg] about-you 用 spinbutton 生日填写 (%d)", cnt)
                for i in range(cnt):
                    sb = spinbuttons.nth(i)
                    try:
                        label = (sb.get_attribute("aria-label") or "").lower()
                    except Exception:
                        label = ""
                    if "year" in label or "yyyy" in label or "年" in label:
                        sb.click()
                        _sleep(0.2)
                        page.keyboard.type(birth["year"], delay=60)
                    elif "month" in label or "mm" in label or "月" in label:
                        sb.click()
                        _sleep(0.2)
                        page.keyboard.type(birth["month"], delay=60)
                    elif "day" in label or "dd" in label or "日" in label:
                        sb.click()
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

    # 2) 点手机登录 — 中英文双语
    if not _click_button_by_text(page, PHONE_LOGIN_TEXTS, timeout_ms=10000):
        safe_screenshot(page, SCREENSHOT_DIR / "phone_02a_no_phone_login.png")
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
            # 后续步骤(用户描述的真实流程:填手机号 → 提交 → 创建密码页 → ... → SMS 验证)
            try:
                is_pw_page = page.locator(PASSWORD_INPUT_SELECTOR).first.is_visible(timeout=2000)
            except Exception:
                is_pw_page = False
            try:
                cur_url = (page.url or "").lower()
            except Exception:
                cur_url = ""
            on_about_you = ("about-you" in cur_url) or ("about_you" in cur_url)
            on_main = ("chatgpt.com" in cur_url and "auth.openai.com" not in cur_url
                       and "/auth/" not in cur_url)

            if is_pw_page or on_about_you or on_main:
                logger.info(
                    "[phone-reg] SMS 框未出现但已进入后续步骤(pw=%s about-you=%s main=%s)"
                    " — break 让主循环接管 attempt=%d url=%s",
                    is_pw_page, on_about_you, on_main, attempt, cur_url[:80],
                )
                safe_screenshot(page, SCREENSHOT_DIR / f"phone_05b_skipped_sms_a{attempt}.png")
                break  # 跳出 SMS retry 循环,进 phase1 主循环
            else:
                logger.warning("[phone-reg] 提交手机号后 15s 未见 code 框也未见后续页 — 号可能被拒")
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
        except sms_mod.SmsTimeout as exc:
            logger.warning("[phone-reg] SMS#1 超时 attempt=%d %ds 死号 ban",
                           attempt, SMS_WAIT_SECONDS)
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

        # 诊断:每轮记录 url + input 摘要(只有 url 变化才打印,避免刷屏)
        if url != last_url:
            input_types = [i.get("type") for i in inputs_info if i.get("type")]
            logger.info(
                "[phone-reg] [DIAG] phase1 r%d url=%s inputs=%s body[:120]=%r",
                round_idx, url[:120], input_types[:8], (body_text or "")[:120],
            )

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

        is_pw_page = ("password" in url) or any(i.get("type") == "password" for i in inputs_info)
        if last_url == url and not is_pw_page:
            continue

        # 密码页
        if is_pw_page:
            diag_counts["pw_hit"] += 1
            logger.info(
                "[phone-reg] [DIAG] phase1 r%d 命中密码页(#%d) url=%s",
                round_idx, diag_counts["pw_hit"], url[:80],
            )
            _fill_password_input(page, password)
            _click_submit_button(page)
            _sleep(3)
            wait_cloudflare(page, max_wait_seconds=60)
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
            logger.info("[phone-reg] phase1 r%d 命中 SMS 验证页,等 SMS#1 order=%d",
                        round_idx, order.id)
            try:
                code = sms_provider.wait_for_otp(
                    order_id=order.id, timeout=SMS_WAIT_SECONDS,
                    should_stop=is_stop_requested,
                )
            except sms_mod.SmsAborted:
                raise BatchStopped("phone reg phase1 SMS 等待时收到 stop")
            except sms_mod.SmsTimeout as exc:
                logger.warning("[phone-reg] phase1 r%d SMS 超时 %ds — 死号 ban", round_idx, SMS_WAIT_SECONDS)
                _safe_refund(sms_provider, order, "ban")
                raise RegisterBlocked("phone_reg", f"SMS#1 超时: {exc}", is_phone=True) from exc

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

    返回是否成功(callback 已捕获到 code 由调用方 capture_callback 检测)。
    """
    logger.info("[phone-reg] === Phase 2 OAuth 开始 ===")
    safe_screenshot(page, SCREENSHOT_DIR / "phone_20_phase2_start.png")

    # cloud-mail baseline:用于 add-email 后的 OTP 不抓到老邮件
    try:
        mail_baseline_id = mail_client.latest_mail_id(email_for_bind)
    except Exception:
        mail_baseline_id = 0
    logger.info("[phone-reg] cloud-mail baseline id=%d for %s", mail_baseline_id, email_for_bind)

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
            logger.info("[phone-reg] ✅ Phase 2 callback 已捕获")
            return True

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

        # 4) /add-email 邮箱绑定
        if "add-email" in url_low or "add_email" in url_low:
            logger.info("[phone-reg] phase2 r%d /add-email,绑定 %s", round_idx, email_for_bind)
            try:
                ei = page.locator(
                    'input[type="email"], input[name="email"], input[name="username"], '
                    'input[name="identifier"]',
                ).first
                ei.wait_for(state="visible", timeout=5000)
                ei.click(click_count=3)
                page.keyboard.type(email_for_bind, delay=30)
            except Exception as exc:
                safe_screenshot(page, SCREENSHOT_DIR / "phone_25_add_email_no_input.png")
                raise OAuthFailed(f"/add-email 找不到 email 输入框: {exc}") from exc
            _sleep(0.5)
            _click_submit_button(page)
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
    password: str,
    mail_client,
) -> dict:
    """手机号注册一体化入口:浏览器开 → 注册 → OAuth → 拿 codex bundle → 关浏览器。

    参数:
      email      — 用于 OAuth /add-email 绑定的 cloud-mail 邮箱(预先 create_email 出来)
      password   — 设给账号的密码(可能 phase2 不用,但 phase1 必填)
      mail_client — cloud-mail 客户端(收 add-email 后的 OTP)

    返回 bundle 同 fetch_personal_bundle:
      {access_token, refresh_token, id_token, account_id, email, plan_type,
       expires_at, phone_verified=True, phone}

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
    logger.info(
        "[phone-reg] 启动 email=%s provider=%s sms_country=%s phone_country=ISO=%s dial=+%s",
        email, sms_provider.PROVIDER_NAME, sms_cfg.get("country"),
        country.iso_code, country.dial_code,
    )

    # PKCE + state + auth URL
    code_verifier, code_challenge = _pkce()
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(code_challenge, state)
    logger.info("[phone-reg] OAuth state=%s redirect=%s", state[:8], CODEX_REDIRECT_URI)

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

        page.on("request", lambda req: _try_extract_code(req.url, "request"))
        page.on("requestfailed", lambda req: _try_extract_code(req.url, "requestfailed"))
        page.on("response", lambda res: _try_extract_code(res.url, "response"))
        page.on("framenavigated", lambda f: _try_extract_code(f.url, "framenav"))

        def _capture() -> bool:
            try:
                _try_extract_code(page.url or "", "live_url")
            except Exception:
                pass
            return auth_code[0] is not None

        try:
            # Phase 1: chatgpt.com 手机号注册
            order, phone_e164 = _phase1_signup(
                page, sms_provider, sms_cfg, country, password, full_name, birth,
            )

            # Phase 2: auth.openai.com OAuth
            _phase2_oauth(
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

            # 交换 token
            bundle = _exchange_code(auth_code[0], code_verifier, fallback_email=email)
            bundle["phone_verified"] = True
            bundle["phone"] = phone_e164

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
