"""hero-sms.com 实现 — sms-activate.org 兼容协议(GET ?api_key=&action=)。

所有调用走同一个 endpoint:GET https://hero-sms.com/stubs/handler_api.php
查询参数:api_key=XXX & action=getNumberV2/getStatus/setStatus/getBalance/...

key actions:
  getBalance      → "ACCESS_BALANCE:12.34" 字符串
  getNumberV2     → JSON {activationId, phoneNumber, activationCost, currency, countryCode, ...}
                    错误字符串 NO_BALANCE / NO_NUMBERS / BAD_KEY / WRONG_SERVICE
  getStatus       → "STATUS_WAIT_CODE" / "STATUS_OK:1234" / "STATUS_CANCEL" / "STATUS_WAIT_RETRY"
                    (用 V1 — V2 的 string 响应会被 SDK 当 error 抛, 不适合轮询)
  setStatus id=X  → status:
                      1 = ready (扣费完成)
                      3 = resend (再要一条 SMS)
                      6 = cancel (2 分钟内 → 全额退;之后部分退或不退)
                      8 = ban (号无效 → 全退 + 不再分配)
                    回 ACCESS_READY / ACCESS_CANCEL / ACCESS_RETRY_GET / EARLY_CANCEL_DENIED ...

国家代码是 sms-activate 行规的 numeric ID:
  0 = Russia, 1 = Ukraine, 2 = Kazakhstan, 6 = Indonesia, 16 = England (UK),
  22 = India, 78 = France, 117 = Portugal, 187 = USA, ...

服务代码:OpenAI/ChatGPT 用 "ot"。

我们的 friendly 字符串("england" / "france" / ...)在 _country_to_id 里翻译成数字。
不认得的字符串若本身就是数字,直接当 ID 用。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import requests

from autofree.core.sms_providers.base import SmsProvider
from autofree.core.sms_types import SmsOrder

logger = logging.getLogger(__name__)

BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
DEFAULT_TIMEOUT = 30
DEFAULT_POLL_INTERVAL = 5

# friendly 名 → sms-activate 数字 ID。覆盖常见 + 用户实际可能用的。
# 不在这表里的字符串若是纯数字就直接用,否则 raise。
COUNTRY_NAME_TO_ID = {
    "russia": 0, "ru": 0,
    "ukraine": 1, "ua": 1,
    "kazakhstan": 2, "kz": 2,
    "china": 3, "cn": 3,
    "philippines": 4, "ph": 4,
    "myanmar": 5, "mm": 5,
    "indonesia": 6, "id": 6,
    "malaysia": 7, "my": 7,
    "kenya": 8, "ke": 8,
    "tanzania": 9, "tz": 9,
    "vietnam": 10, "vn": 10,
    "kyrgyzstan": 11, "kg": 11,
    "usa": 12, "us": 12, "america": 12,  # 主 USA 池;另有 187 = USA premium
    "israel": 13, "il": 13,
    "hongkong": 14, "hk": 14,
    "poland": 15, "pl": 15,
    "england": 16, "uk": 16, "britain": 16, "gb": 16,
    "madagascar": 17, "mg": 17,
    "drcongo": 18,
    "nigeria": 19, "ng": 19,
    "macao": 20, "mo": 20,
    "egypt": 21, "eg": 21,
    "india": 22, "in": 22,
    "ireland": 23, "ie": 23,
    "cambodia": 24, "kh": 24,
    "laos": 25, "la": 25,
    "haiti": 26, "ht": 26,
    "ivorycoast": 27, "ci": 27,
    "gambia": 28, "gm": 28,
    "serbia": 29, "rs": 29,
    "yemen": 30, "ye": 30,
    "southafrica": 31, "za": 31,
    "romania": 32, "ro": 32,
    "colombia": 33, "co": 33,
    "estonia": 34, "ee": 34,
    "azerbaijan": 35, "az": 35,
    "canada": 36, "ca": 36,
    "morocco": 37, "ma": 37,
    "ghana": 38, "gh": 38,
    "argentina": 39, "ar": 39,
    "uzbekistan": 40, "uz": 40,
    "cameroon": 41, "cm": 41,
    "chad": 42, "td": 42,
    "germany": 43, "de": 43,
    "lithuania": 44, "lt": 44,
    "croatia": 45, "hr": 45,
    "sweden": 46, "se": 46,
    "iraq": 47, "iq": 47,
    "netherlands": 48, "nl": 48,
    "latvia": 49, "lv": 49,
    "austria": 50, "at": 50,
    "belarus": 51, "by": 51,
    "thailand": 52, "th": 52,
    "saudiarabia": 53, "sa": 53,
    "mexico": 54, "mx": 54,
    "taiwan": 55, "tw": 55,
    "spain": 56, "es": 56,
    "iran": 57, "ir": 57,
    "algeria": 58, "dz": 58,
    "slovenia": 59, "si": 59,
    "bangladesh": 60, "bd": 60,
    "senegal": 61, "sn": 61,
    "turkey": 62, "tr": 62,
    "czech": 63, "cz": 63,
    "srilanka": 64, "lk": 64,
    "peru": 65, "pe": 65,
    "pakistan": 66, "pk": 66,
    "newzealand": 67, "nz": 67,
    "guinea": 68, "gn": 68,
    "mali": 69, "ml": 69,
    "venezuela": 70, "ve": 70,
    "ethiopia": 71, "et": 71,
    "mongolia": 72, "mn": 72,
    "brazil": 73, "br": 73,
    "afghanistan": 74, "af": 74,
    "uganda": 75, "ug": 75,
    "angola": 76, "ao": 76,
    "cyprus": 77, "cy": 77,
    "france": 78, "fr": 78,
    "papuanewguinea": 79, "pg": 79,
    "mozambique": 80, "mz": 80,
    "nepal": 81, "np": 81,
    "belgium": 82, "be": 82,
    "bulgaria": 83, "bg": 83,
    "hungary": 84, "hu": 84,
    "moldova": 85, "md": 85,
    "italy": 86, "it": 86,
    "paraguay": 87, "py": 87,
    "honduras": 88, "hn": 88,
    "tunisia": 89, "tn": 89,
    "nicaragua": 90, "ni": 90,
    "timorleste": 91, "tl": 91,
    "bolivia": 92, "bo": 92,
    "costarica": 93, "cr": 93,
    "guatemala": 94, "gt": 94,
    "uae": 95, "ae": 95,
    "zimbabwe": 96, "zw": 96,
    "puertorico": 97, "pr": 97,
    "sudan": 98, "sd": 98,
    "togo": 99, "tg": 99,
    # 有些站把 USA premium 放 187,把 12 给 SIP/老池
    "usapremium": 187,
    # 中国大陆别名(如果开放)
    "中国": 3,
    "英国": 16, "英格兰": 16,
    "美国": 12,
    "印度": 22,
    "法国": 78,
    "印尼": 6, "印度尼西亚": 6,
    "俄罗斯": 0,
    "乌克兰": 1,
}

# 服务代码 — 注意 hero-sms 与 sms-activate 标准**不一样**(尤其 OpenAI):
#   hero-sms:  dr = OpenAI,  ot = "Any other"(杂项 fallback,贵 3 倍)
#   sms-act:   ot = OpenAI
# 已用 /api/v1/left-menu/services 实际验证(2026-05)。其它常用 service 两边一致。
SERVICE_NAME_TO_CODE = {
    "openai": "dr", "chatgpt": "dr", "gpt": "dr",
    "google": "go", "gmail": "go", "youtube": "go",
    "telegram": "tg",
    "whatsapp": "wa",
    "facebook": "fb",
    "discord": "ds",
    "twitter": "tw", "x": "tw",
    "instagram": "ig",
    "tiktok": "lf",
    "uber": "ub",
}


def _country_to_id(country: str) -> int:
    """friendly 名 → 数字 ID。已是纯数字直接用;不认得 raise。

    宽松匹配:去空格 / 连字符 / 下划线 — `south africa` / `South-Africa` / `south_africa` 都识别。
    """
    s = (country or "").strip().lower()
    if not s:
        return 16  # 默认 England
    if s.isdigit():
        return int(s)
    # 先 raw 查一遍(包含 2 字母代码 ru/ua 这种,不能误删空白)
    if s in COUNTRY_NAME_TO_ID:
        return COUNTRY_NAME_TO_ID[s]
    # 退回宽松匹配:去掉空白 / 连字符 / 下划线
    normalized = s.replace(" ", "").replace("-", "").replace("_", "")
    if normalized and normalized in COUNTRY_NAME_TO_ID:
        return COUNTRY_NAME_TO_ID[normalized]
    from autofree.core.sms import SmsConfigMissing
    raise SmsConfigMissing(
        f"hero-sms: 不认识国家 {country!r} — 请用英文名(england/france/usa/southafrica)"
        f"或 2 字母代码(gb/fr/us/za)或数字 ID(England=16 / France=78 / SouthAfrica=31)"
    )


def _service_to_code(product: str) -> str:
    s = (product or "openai").strip().lower()
    return SERVICE_NAME_TO_CODE.get(s, s)  # 不认得就直接传(用户可能给原始 code 如 "ot")


def _country_id_to_name(cid: int) -> str:
    """数字 ID 反向查名字 — 仅用于日志可读性,找不到就返字符串形式。"""
    for name, val in COUNTRY_NAME_TO_ID.items():
        if val == cid and len(name) > 2 and name.isascii():  # 跳过 "ru"/"ua" 这种 2 字母代码
            return name
    return str(cid)


class HeroSmsProvider(SmsProvider):
    PROVIDER_NAME = "hero-sms"
    DEFAULT_COUNTRY = "england"   # 用户指定 — 0.015 USD/号
    DEFAULT_OPERATOR = ""         # hero-sms 默认空 = 任意
    DEFAULT_SERVICE = "openai"

    def _api(self, action: str, params: dict | None = None) -> str:
        """统一 GET ?api_key=&action= 调用。返回原始 text(调用方决定 JSON/string 解析)。"""
        from autofree.core.sms import SmsConfigMissing, SmsError
        q = {"api_key": self.api_key, "action": action}
        if params:
            q.update({k: str(v) for k, v in params.items()})
        try:
            resp = requests.get(BASE_URL, params=q, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise SmsError(f"[hero-sms] 网络异常 action={action}: {exc}") from exc
        if resp.status_code != 200:
            # HTTP 400 + WRONG_MAX_PRICE:用户设的 max_price 低于该国家/服务的最低价。
            # 重试无意义(每次都同样错),用 SmsConfigMissing 直接抛出去到 batch 层。
            if resp.status_code == 400 and "WRONG_MAX_PRICE" in resp.text:
                try:
                    import json as _json
                    body = _json.loads(resp.text)
                    minp = (body.get("info") or {}).get("min")
                    sent = (params or {}).get("maxPrice")
                    raise SmsConfigMissing(
                        f"[hero-sms] max_price=${sent} 低于该国家/服务最低价 "
                        f"${minp} — 请调高 max_price 或换国家/服务"
                    )
                except SmsConfigMissing:
                    raise
                except Exception:
                    pass
            raise SmsError(f"[hero-sms] HTTP {resp.status_code} action={action}: {resp.text[:200]}")
        text = resp.text.strip()
        if text in ("BAD_KEY", "NO_KEY"):
            raise SmsConfigMissing(f"[hero-sms] api_key 无效 ({text})")
        if text.startswith("ERROR_SQL"):
            raise SmsError(f"[hero-sms] 服务端错误 {text}")
        return text

    def get_balance(self) -> dict:
        text = self._api("getBalance")
        # 成功格式: "ACCESS_BALANCE:12.345"
        if text.startswith("ACCESS_BALANCE:"):
            try:
                bal = float(text.split(":", 1)[1].strip())
            except ValueError:
                bal = None
            return {
                "provider": self.PROVIDER_NAME,
                "balance": bal,
                "currency": "USD",
                "raw": text,
            }
        from autofree.core.sms import SmsError
        raise SmsError(f"[hero-sms] getBalance 异常返回: {text}")

    def _query_market(self, svc: str, cid: int) -> dict | None:
        """查询 Free Price 池实时市场数据(hero-sms 前端用的公开 endpoint,无需 api_key)。

        返回示例:
          {"min": 0.025, "default": 0.025, "avg": 0.0246, "maxAvailable": 0.2942,
           "totalCount": 38842, "operators": [{"name": "tim", "min": 0.0185, "count": 5977}, ...]}
        失败/不可用返回 None — 调用方继续走原 buy 流程,不阻塞。
        """
        try:
            url = f"https://hero-sms.com/api/v1/left-menu/service/{svc}/country/{cid}/offers"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return None
            body = resp.json()
            svc_block = (body.get("data") or {}).get(svc) or {}
            fp = svc_block.get("freePrice") or {}
            ops = []
            for op in svc_block.get("operators") or []:
                offers = op.get("freePriceOffers") or {}
                if not offers:
                    continue
                try:
                    prices = sorted(float(k) for k in offers.keys())
                except Exception:
                    continue
                ops.append({
                    "name": op.get("name"),
                    "min": prices[0] if prices else None,
                    "count": op.get("activationsCount") or 0,
                })
            # hero-sms 是双轨制:
            #   - handler_api(api_key 鉴权,我们用这个):真实下限是 freePrice.min
            #   - /api/v1 marketplace(session 登录,网页用户):可到 maxRank /
            #     operators[].freePriceOffers 的最低价,显著更便宜
            # API 用户买不到 marketplace 池。所以预检用 freePrice.min 才对。
            marketplace_min = min(
                (o["min"] for o in ops if o.get("min") is not None), default=None,
            )
            return {
                "min": float(fp.get("min")) if fp.get("min") is not None else None,
                "default": float(fp.get("default")) if fp.get("default") is not None else None,
                "avg": float(fp.get("avg")) if fp.get("avg") is not None else None,
                "maxAvailable": float(fp.get("maxAvailable")) if fp.get("maxAvailable") is not None else None,
                "maxRank": float(fp.get("maxRank")) if fp.get("maxRank") is not None else None,
                "totalCount": sum(o.get("count", 0) for o in ops),
                "operators": ops,
                "marketplace_min": marketplace_min,
            }
        except Exception as exc:
            logger.debug("[hero-sms] 市场预检失败 svc=%s cid=%s: %s", svc, cid, exc)
            return None

    def buy_activation(
        self, *, country: str, operator: str, product: str,
        max_price: float | None = None,
    ) -> SmsOrder:
        from autofree.core.sms import SmsBuyFailed, SmsConfigMissing
        cid = _country_to_id(country)
        svc = _service_to_code(product)
        params: dict = {"service": svc, "country": cid}
        if operator and operator.strip().lower() not in ("", "any"):
            params["operator"] = operator.strip().lower()

        # 设了 max_price 时,用公开 endpoint 预检 — 避免被 hero-sms HTTP 400 拒绝。
        # 真实 API 下限是 freePrice.min(不是 operators[].min)— marketplace 那些 $0.03
        # 的便宜号只能网页登录买,API 拿不到。
        if max_price is not None and max_price > 0:
            market = self._query_market(svc, cid)
            if market:
                api_min = market.get("min")  # freePrice.min,API 真实下限
                mp_min = market.get("marketplace_min")  # operators 最低,仅网页可买
                logger.info(
                    "[hero-sms] 市场预检 svc=%s country=%s API最低=$%s "
                    "网页marketplace最低=$%s avg=$%s 你限价=$%s",
                    svc, cid, api_min, mp_min, market.get("avg"), max_price,
                )
                if api_min is not None and max_price < api_min:
                    mp_hint = (f"(网页 marketplace 池实际最低 ${mp_min},但 API 拿不到 — "
                               f"如要 ${mp_min} 这种价位需登录 hero-sms 网页手动买)"
                               if mp_min is not None and mp_min < api_min else "")
                    raise SmsConfigMissing(
                        f"[hero-sms] max_price=${max_price} 低于 API 最低价 "
                        f"${api_min}{mp_hint} — 请调高 max_price 到 ≥${api_min} 或换国家/服务"
                    )
        # max_price=0.080 → 只接 ≤$0.080 的号,贵的拒收(sms-activate 协议 maxPrice)。
        # 注意:hero-sms 不开放 marketplace 给 handler_api,API 真实下限 = freePrice.min
        # (实测带 freePrice=true 没用)。网页上 $0.025 的便宜号 API 拿不到。
        if max_price is not None and max_price > 0:
            params["maxPrice"] = f"{max_price:.4f}".rstrip("0").rstrip(".")
            logger.info("[hero-sms] 限价 maxPrice=$%s (注:marketplace 价不在 API 范围)",
                        params["maxPrice"])

        text = self._api("getNumberV2", params)

        # 错误字符串 — 业务层失败
        if text.startswith("NO_BALANCE"):
            raise SmsBuyFailed(f"[hero-sms] 余额不足 (country={cid} service={svc})")
        if text.startswith("NO_NUMBERS"):
            raise SmsBuyFailed(f"[hero-sms] 库存空 (country={cid} service={svc} operator={operator!r})")
        if text.startswith("WRONG_SERVICE"):
            raise SmsBuyFailed(f"[hero-sms] service={svc} 无效")
        if text.startswith("BANNED:"):
            raise SmsBuyFailed(f"[hero-sms] 账户被封 — {text}")
        if text.startswith("ERROR_") or text in ("WRONG_OPERATOR", "WRONG_COUNTRY"):
            raise SmsBuyFailed(f"[hero-sms] 下单失败: {text}")

        # 旧 ACCESS_NUMBER:<id>:<phone> 兼容(部分子站还在用)
        if text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":")
            if len(parts) >= 3:
                order = SmsOrder(
                    id=int(parts[1]),
                    phone=parts[2] if parts[2].startswith("+") else f"+{parts[2]}",
                    country=_country_id_to_name(cid),
                    operator=operator or "",
                    product=svc,
                    price=0.0,
                    expires_at=time.time() + 20 * 60,
                    provider=self.PROVIDER_NAME,
                )
                logger.info(
                    "[hero-sms] 下单成功 (legacy) id=%s phone=%s country=%s service=%s",
                    order.id, order.phone, order.country, svc,
                )
                return order

        # JSON 路径
        try:
            import json
            data = json.loads(text)
        except Exception as exc:
            raise SmsBuyFailed(f"[hero-sms] getNumberV2 无法解析: {text[:200]} ({exc})") from exc

        if isinstance(data, dict) and data.get("status") == "error":
            raise SmsBuyFailed(f"[hero-sms] {data.get('error') or data.get('msg') or 'error'}")

        if not isinstance(data, dict) or "activationId" not in data:
            raise SmsBuyFailed(f"[hero-sms] 响应结构异常: {data}")

        phone = str(data.get("phoneNumber") or "")
        if phone and not phone.startswith("+"):
            phone = "+" + phone
        order = SmsOrder(
            id=int(data["activationId"]),
            phone=phone,
            country=_country_id_to_name(int(data.get("countryCode") or cid)),
            operator=str(data.get("activationOperator") or operator or ""),
            product=svc,
            price=float(data.get("activationCost") or 0),
            expires_at=time.time() + 20 * 60,  # hero-sms 默认 20 分钟有效
            provider=self.PROVIDER_NAME,
        )
        logger.info(
            "[hero-sms] 下单成功 id=%s phone=%s country=%s operator=%s price=$%.4f",
            order.id, order.phone, order.country, order.operator, order.price,
        )
        return order

    def _set_status(self, order_id: int, status: int) -> str:
        """status: 1=ready(扣费完成) / 3=resend / 6=cancel / 8=ban。"""
        return self._api("setStatus", {"id": order_id, "status": status})

    def finish_order(self, order_id: int) -> None:
        try:
            r = self._set_status(order_id, 1)
            logger.debug("[hero-sms] finish %s -> %s", order_id, r)
        except Exception as exc:
            logger.warning("[hero-sms] finish 失败 id=%s: %s", order_id, exc)

    def cancel_order(self, order_id: int) -> None:
        try:
            r = self._set_status(order_id, 6)
            if r.startswith("EARLY_CANCEL_DENIED"):
                logger.warning("[hero-sms] cancel 被拒(2 分钟内禁止取消) id=%s", order_id)
            else:
                logger.debug("[hero-sms] cancel %s -> %s", order_id, r)
        except Exception as exc:
            logger.warning("[hero-sms] cancel 失败 id=%s: %s", order_id, exc)

    def ban_order(self, order_id: int) -> None:
        try:
            r = self._set_status(order_id, 8)
            logger.debug("[hero-sms] ban %s -> %s", order_id, r)
        except Exception as exc:
            logger.warning("[hero-sms] ban 失败 id=%s: %s", order_id, exc)

    def wait_for_otp(
        self,
        *,
        order_id: int,
        timeout: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        """轮询 getStatus 直到拿到 STATUS_OK:<code>。

        可能的 status 字符串:
          STATUS_WAIT_CODE     初始 - 等 SMS
          STATUS_WAIT_RETRY:X  收到无效 SMS, 等下条
          STATUS_WAIT_RESEND   等用户主动 resend
          STATUS_OK:1234       SMS 到 - 含 code
          STATUS_CANCEL        号已被取消
          NO_ACTIVATION        订单不存在
        """
        from autofree.core.sms import SmsAborted, SmsError, SmsTimeout
        deadline = time.time() + timeout
        last_log = 0.0
        while time.time() < deadline:
            if should_stop and should_stop():
                raise SmsAborted(f"[hero-sms] 等 OTP 被外部中断 order_id={order_id}")
            try:
                text = self._api("getStatus", {"id": order_id})
            except SmsError as exc:
                logger.warning("[hero-sms] poll 异常 id=%s: %s — 继续轮询", order_id, exc)
                text = ""
            if text.startswith("STATUS_OK"):
                # "STATUS_OK:1234" 或 "STATUS_OK_X:1234"
                parts = text.split(":", 1)
                if len(parts) == 2 and parts[1].strip():
                    code = parts[1].strip()
                    # 偶有 SMS 全文跑出来,抠数字
                    m = re.search(r"\b(\d{4,8})\b", code)
                    code = m.group(1) if m else code
                    logger.info("[hero-sms] 收到 OTP order_id=%d code=%s", order_id, code)
                    return code
            elif text == "STATUS_CANCEL":
                raise SmsError(f"[hero-sms] 号已被取消 order_id={order_id}")
            elif text == "NO_ACTIVATION":
                raise SmsError(f"[hero-sms] 订单不存在 order_id={order_id}")
            # 其它都视为 still waiting (STATUS_WAIT_CODE / STATUS_WAIT_RETRY / 空)

            now = time.time()
            if now - last_log > 15:
                logger.info("[hero-sms] 等 OTP order_id=%d status=%s remain=%ds",
                            order_id, text or "(empty)", int(deadline - now))
                last_log = now
            slept = 0
            while slept < DEFAULT_POLL_INTERVAL:
                if should_stop and should_stop():
                    raise SmsAborted(f"[hero-sms] 等 OTP 被外部中断 order_id={order_id}")
                time.sleep(1)
                slept += 1
        raise SmsTimeout(f"[hero-sms] 等 OTP 超时 order_id={order_id} timeout={timeout}s")
