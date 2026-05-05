"""SMS provider 间共享的 dataclass — 拆出来避免 base ↔ sms 循环导入。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SmsOrder:
    """所有 provider 下单成功后返回的统一订单结构。

    fields:
      id        provider 内部订单 ID(int)
      phone     完整 E.164 格式手机号,带 + 前缀
      country   provider 实际给的国家(标准化 — 5sim 给 slug 如 "france",
                hero-sms 给 ISO/numeric 时 provider 内部翻译成可读字符串)
      operator  provider 实际给的运营商(可空)
      product   服务名("openai" 等)
      price     单价 USD(provider 各自折算)
      expires_at epoch sec(若 provider 不返,默认下单 + 20 分钟)
      provider  本次订单来自哪个 provider(冗余字段,日志/审计用)
    """

    id: int
    phone: str
    country: str
    operator: str
    product: str
    price: float
    expires_at: float
    provider: str = ""
