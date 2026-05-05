"""SMS 接码 facade — 错误类 + dataclass 重导出 + provider 工厂。

调用方(oauth/_solve_phone_gate)只用 `get_active_provider()` 拿 provider 对象,
然后调 `provider.buy_activation(...)` / `wait_for_otp(...)` / `cancel_order(...)`。
具体走 5sim 还是 hero-sms 由 runtime_config(panel)决定。

为了向后兼容(纯 freegen CLI / 旧测试),保留模块级 `wait_for_otp/buy_activation/...`
函数,内部委托给 active provider — 已废弃,新代码请直接走 provider。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

# 公共 dataclass(provider 间共享,放独立模块避免循环导入)
from autofree.core.sms_types import SmsOrder  # noqa: F401  (re-export for back-compat)

logger = logging.getLogger(__name__)

# 单号最长等几秒 OTP — 用户实测 90s 没到基本是死号,继续等纯浪费时间
DEFAULT_WAIT_OTP_SECONDS = 90


# ─── 错误类 ───────────────────────────────────────────────────────────────
class SmsError(Exception):
    """SMS 调用通用异常基类。"""


class SmsConfigMissing(SmsError):
    """缺少 api_key / 国家代码无效 等配置类问题。"""


class SmsBuyFailed(SmsError):
    """下单失败(库存空、余额不足、参数错等)。"""


class SmsTimeout(SmsError):
    """轮询超时,SMS 一直没到。"""


class SmsAborted(SmsError):
    """轮询被外部信号中断(如用户点 Stop)— 调用方应 cancel 当前订单。"""


# ─── provider 工厂 ───────────────────────────────────────────────────────
def get_active_provider(cfg: dict | None = None):
    """根据 cfg(或 freegen.config.get_sms_config 默认)实例化 active provider。

    cfg 必须含: provider, api_key
    可选: country, operator, service(provider 自己再回退默认值)
    """
    if cfg is None:
        from autofree.core.config import get_sms_config
        cfg = get_sms_config()
    name = (cfg.get("provider") or "5sim").strip().lower()
    api_key = cfg.get("api_key", "")

    if name == "5sim":
        from autofree.core.sms_providers.fivesim import FiveSimProvider
        return FiveSimProvider(api_key=api_key)
    if name in ("hero-sms", "herosms", "hero_sms"):
        from autofree.core.sms_providers.herosms import HeroSmsProvider
        return HeroSmsProvider(api_key=api_key)

    raise SmsConfigMissing(
        f"未知 SMS provider: {name!r} — 支持: 5sim / hero-sms"
    )


# ─── 向后兼容的模块级函数(已废弃,新代码请用 get_active_provider) ──────────────
# 这些函数的旧签名带 api_key 参数,现在 api_key 走 active provider,旧 api_key 参数被忽略。
# 保留是为了兼容历史 /sms-balance 调用风格 + 可能存在的外部 CLI / 测试代码。

def get_balance(api_key: str | None = None) -> dict:
    """已废弃 — 改用 get_active_provider().get_balance()。"""
    return get_active_provider().get_balance()


def buy_activation(*, api_key: str | None = None, country: str, operator: str, product: str) -> SmsOrder:
    """已废弃 — 改用 get_active_provider().buy_activation(...)。"""
    return get_active_provider().buy_activation(country=country, operator=operator, product=product)


def wait_for_otp(
    *,
    api_key: str | None = None,
    order_id: int,
    timeout: int = DEFAULT_WAIT_OTP_SECONDS,
    poll_interval: int = 5,  # noqa: ARG001  (provider 内部固定,这个参数已无意义)
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """已废弃 — 改用 get_active_provider().wait_for_otp(...)。"""
    return get_active_provider().wait_for_otp(
        order_id=order_id, timeout=timeout, should_stop=should_stop,
    )


def cancel_order(api_key: str | None, order_id: int) -> None:
    """已废弃 — 改用 get_active_provider().cancel_order(order_id)。"""
    get_active_provider().cancel_order(order_id)


def ban_order(api_key: str | None, order_id: int) -> None:
    """已废弃 — 改用 get_active_provider().ban_order(order_id)。"""
    get_active_provider().ban_order(order_id)


def finish_order(api_key: str | None, order_id: int) -> None:
    """已废弃 — 改用 get_active_provider().finish_order(order_id)。"""
    get_active_provider().finish_order(order_id)


def country_to_dial_code(country: str) -> str:
    """5sim country slug → 国家拨号码 — UI/日志辅助,不参与流程。

    返回 "" 表示未知。
    """
    table = {
        "india": "+91", "indonesia": "+62", "russia": "+7", "kazakhstan": "+7",
        "ukraine": "+380", "poland": "+48", "germany": "+49", "france": "+33",
        "spain": "+34", "uk": "+44", "england": "+44", "britain": "+44",
        "usa": "+1", "philippines": "+63", "vietnam": "+84", "pakistan": "+92",
        "brazil": "+55", "mexico": "+52", "argentina": "+54",
    }
    return table.get((country or "").lower(), "")
