"""SmsProvider 抽象基类 — 所有 provider 的公共契约。

要点:
- buy_activation 必须返回 SmsOrder dataclass(共享于 freegen.sms),失败 raise SmsBuyFailed
- wait_for_otp 接受 should_stop callable,响应延迟 ≤ 1s;超时 raise SmsTimeout;中断 raise SmsAborted
- cancel/ban/finish 失败只 log warning,不抛(best-effort 退款)
- get_balance 返回 dict(provider 自决字段,UI 显示原文)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from autofree.core.sms_types import SmsOrder


class SmsProvider(ABC):
    """所有 SMS 接码 provider 实现这个抽象。"""

    PROVIDER_NAME: str = ""           # "5sim" / "hero-sms" / ...
    DEFAULT_COUNTRY: str = ""         # provider 默认国家(用户没配时兜底)
    DEFAULT_OPERATOR: str = ""        # provider 默认运营商
    DEFAULT_SERVICE: str = "openai"   # provider 默认服务名(各自翻译为内部代码)

    def __init__(self, api_key: str):
        if not api_key:
            from autofree.core.sms import SmsConfigMissing
            raise SmsConfigMissing(f"{self.PROVIDER_NAME} api_key 未配置")
        self.api_key = api_key

    @abstractmethod
    def buy_activation(self, *, country: str, operator: str, product: str) -> SmsOrder:
        """下单买号。失败 raise SmsBuyFailed。"""

    @abstractmethod
    def wait_for_otp(
        self,
        *,
        order_id: int,
        timeout: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        """轮询 OTP。超时 raise SmsTimeout;should_stop 命中 raise SmsAborted。"""

    @abstractmethod
    def cancel_order(self, order_id: int) -> None:
        """取消订单(SMS 未到 → 全额退款,2 分钟后 → 部分退或不退)。失败只 log。"""

    @abstractmethod
    def ban_order(self, order_id: int) -> None:
        """投诉号无效(全额退 + 标记不再分配同号)。失败只 log。"""

    @abstractmethod
    def finish_order(self, order_id: int) -> None:
        """SMS 收到后调,确认扣费。失败只 log。"""

    @abstractmethod
    def get_balance(self) -> dict:
        """返回 {balance, currency, raw, ...} — 字段按 provider 自决,UI 直接展示。"""
