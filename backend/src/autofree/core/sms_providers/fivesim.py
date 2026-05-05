"""5sim.net 实现 — Bearer auth + REST endpoints。

文档:https://5sim.net/zh/docs

key endpoints (GET, header `Authorization: Bearer <api_key>`,Accept: application/json):
  - GET /v1/user/profile                                        → 余额
  - GET /v1/user/buy/activation/{country}/{operator}/{product}  → 下单
  - GET /v1/user/check/{id}                                     → 轮询
  - GET /v1/user/finish/{id}                                    → 标记完成
  - GET /v1/user/cancel/{id}                                    → 取消(2 分钟内全退)
  - GET /v1/user/ban/{id}                                       → 投诉(全退 + 不再分同号)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

from autofree.core.sms_providers.base import SmsProvider
from autofree.core.sms_types import SmsOrder

logger = logging.getLogger(__name__)

BASE_URL = "https://5sim.net/v1"
DEFAULT_TIMEOUT = 30
DEFAULT_POLL_INTERVAL = 5


class FiveSimProvider(SmsProvider):
    PROVIDER_NAME = "5sim"
    DEFAULT_COUNTRY = "france"
    DEFAULT_OPERATOR = "any"
    DEFAULT_SERVICE = "openai"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def get_balance(self) -> dict:
        from autofree.core.sms import SmsError
        resp = requests.get(f"{BASE_URL}/user/profile", headers=self._headers(), timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            raise SmsError(f"[5sim] get_balance HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return {
            "provider": self.PROVIDER_NAME,
            "balance": data.get("balance"),
            "currency": "USD",
            "email": data.get("email"),
            "rating": data.get("rating"),
            "raw": data,
        }

    def buy_activation(self, *, country: str, operator: str, product: str) -> SmsOrder:
        from autofree.core.sms import SmsBuyFailed
        country = (country or self.DEFAULT_COUNTRY).strip().lower() or self.DEFAULT_COUNTRY
        operator = (operator or self.DEFAULT_OPERATOR).strip().lower() or self.DEFAULT_OPERATOR
        product = (product or self.DEFAULT_SERVICE).strip().lower() or self.DEFAULT_SERVICE
        url = f"{BASE_URL}/user/buy/activation/{country}/{operator}/{product}"
        resp = requests.get(url, headers=self._headers(), timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            raise SmsBuyFailed(
                f"[5sim] buy HTTP {resp.status_code} country={country} operator={operator} "
                f"product={product}: {resp.text[:200]}"
            )
        # ⚠ 5sim 失败时返 HTTP 200 + 纯文本(NOT JSON), 必须先判 text 再尝试 json.
        # 已知错误字符串(5sim docs):
        #   "no free phones"           — 库存空(常见: virtual51 跑光)
        #   "no phones"                — 同上
        #   "not enough user balance"  — 余额不足
        #   "no product"               — service 名错
        #   "country is incorrect"     — country slug 错
        #   "operator not found"       — operator 名错
        text = resp.text or ""
        if not text.lstrip().startswith("{"):
            # 纯文本 = 失败. 把原文直接抛给上层, 用户能立刻看出真因
            raise SmsBuyFailed(
                f"[5sim] buy 失败 — 5sim 原文 {text!r} (country={country} operator={operator} "
                f"product={product})"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SmsBuyFailed(
                f"[5sim] buy 响应无法解析为 JSON: {text[:200]!r} ({exc})"
            ) from exc
        if "id" not in data:
            raise SmsBuyFailed(f"[5sim] buy 无 id: {data}")
        expires_str = data.get("expires") or ""
        try:
            import datetime as _dt
            if expires_str.endswith("Z"):
                expires_str = expires_str[:-1] + "+00:00"
            expires_at = _dt.datetime.fromisoformat(expires_str).timestamp()
        except Exception:
            expires_at = time.time() + 20 * 60
        order = SmsOrder(
            id=int(data["id"]),
            phone=data.get("phone", ""),
            country=data.get("country", country),
            operator=data.get("operator", operator),
            product=data.get("product", product),
            price=float(data.get("price", 0)),
            expires_at=expires_at,
            provider=self.PROVIDER_NAME,
        )
        logger.info(
            "[5sim] 下单成功 id=%s phone=%s country=%s operator=%s price=$%.2f",
            order.id, order.phone, order.country, order.operator, order.price,
        )
        return order

    def _check_order(self, order_id: int) -> dict:
        from autofree.core.sms import SmsError
        resp = requests.get(
            f"{BASE_URL}/user/check/{order_id}", headers=self._headers(), timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code != 200:
            raise SmsError(f"[5sim] check HTTP {resp.status_code}: {resp.text[:200]}")
        text = resp.text or ""
        if not text.lstrip().startswith("{"):
            # 5sim 偶发返纯文本(order expired / no activation 等)
            raise SmsError(f"[5sim] check 异常文本 order_id={order_id}: {text[:200]!r}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SmsError(f"[5sim] check JSON 解析失败 order_id={order_id}: {text[:200]!r} ({exc})") from exc

    def finish_order(self, order_id: int) -> None:
        resp = requests.get(f"{BASE_URL}/user/finish/{order_id}", headers=self._headers(), timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("[5sim] finish HTTP %d: %s", resp.status_code, resp.text[:200])

    def cancel_order(self, order_id: int) -> None:
        resp = requests.get(f"{BASE_URL}/user/cancel/{order_id}", headers=self._headers(), timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("[5sim] cancel HTTP %d: %s", resp.status_code, resp.text[:200])

    def ban_order(self, order_id: int) -> None:
        resp = requests.get(f"{BASE_URL}/user/ban/{order_id}", headers=self._headers(), timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("[5sim] ban HTTP %d: %s", resp.status_code, resp.text[:200])

    def wait_for_otp(
        self,
        *,
        order_id: int,
        timeout: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        from autofree.core.sms import SmsAborted, SmsTimeout
        deadline = time.time() + timeout
        last_log = 0.0
        while time.time() < deadline:
            if should_stop and should_stop():
                raise SmsAborted(f"[5sim] 等 OTP 被外部中断 order_id={order_id}")
            data = self._check_order(order_id)
            sms = data.get("sms") or []
            if sms:
                latest = sms[-1]
                code = (latest.get("code") or "").strip()
                if code:
                    logger.info("[5sim] 收到 OTP order_id=%d code=%s sender=%s",
                                order_id, code, latest.get("sender"))
                    return code
                text = latest.get("text") or ""
                import re
                m = re.search(r"\b(\d{4,8})\b", text)
                if m:
                    code = m.group(1)
                    logger.info("[5sim] 从 text 抠出 OTP order_id=%d code=%s text=%r",
                                order_id, code, text[:80])
                    return code
            now = time.time()
            if now - last_log > 15:
                logger.info("[5sim] 等 OTP order_id=%d status=%s sms_count=%d remain=%ds",
                            order_id, data.get("status"), len(sms), int(deadline - now))
                last_log = now
            slept = 0
            while slept < DEFAULT_POLL_INTERVAL:
                if should_stop and should_stop():
                    raise SmsAborted(f"[5sim] 等 OTP 被外部中断 order_id={order_id}")
                time.sleep(1)
                slept += 1
        raise SmsTimeout(f"[5sim] 等 OTP 超时 order_id={order_id} timeout={timeout}s")
