"""Settings API — cloud-mail / sms / cpa 三组配置 + SMS 余额查询。

模型:KV 表 (Setting),分组前缀 cloud_mail. / sms. / cpa.
api_key 类敏感字段:GET 返 mask 串 + has_xxx 布尔;PUT 空串视为不更改。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from autofree.core.config import (
    PROXY_PROVIDER_DEFAULTS,
    PROXY_PROVIDERS_KNOWN,
    SMS_PROVIDERS_KNOWN,
    get_cpa_config,
    get_mail_config,
    get_proxy_config,
    get_sms_config,
    get_sms_provider_config,
    write_setting_group,
)
from autofree.db.base import get_db
from autofree.deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────── helpers ───────────────────────────

def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


def _filter_blank_secret(payload: dict, secret_keys: list[str]) -> dict:
    """空串视为不更改 — 删掉 payload 里值为空的 secret keys。"""
    return {k: v for k, v in payload.items() if not (k in secret_keys and (v == "" or v is None))}


# ─────────────────────────── cloud-mail ───────────────────────────

class CloudMailParams(BaseModel):
    base_url: Optional[str] = None
    password: Optional[str] = None


@router.get("/cloud-mail")
def get_cloud_mail(_user=Depends(require_user)) -> dict:
    cfg = get_mail_config()
    return {
        "base_url": cfg["base_url"],
        "password_masked": _mask(cfg["password"]),
        "has_password": bool(cfg["password"]),
    }


@router.put("/cloud-mail")
def put_cloud_mail(params: CloudMailParams, _user=Depends(require_user)) -> dict:
    body = {k: v for k, v in params.model_dump(exclude_none=True).items()}
    body = _filter_blank_secret(body, ["password"])
    if not body:
        return get_cloud_mail(_user=_user)
    write_setting_group("cloud_mail", body)
    cfg = get_mail_config()
    return {
        "base_url": cfg["base_url"],
        "password_masked": _mask(cfg["password"]),
        "has_password": bool(cfg["password"]),
        "msg": "已保存",
    }


# ─────────────────────────── sms ───────────────────────────

class SmsParams(BaseModel):
    """每个 provider 配置独立 namespace 写入 sms.<provider>.* — 必带 provider。"""
    provider: str
    api_key: Optional[str] = None
    service: Optional[str] = None
    country: Optional[str] = None
    operator: Optional[str] = None
    set_active: Optional[bool] = None  # True = 同时把这个 provider 设为 active


class SmsActiveParams(BaseModel):
    provider: str


def _sms_provider_block(cfg: dict) -> dict:
    return {
        "api_key_masked": _mask(cfg.get("api_key", "")),
        "has_api_key": bool(cfg.get("api_key")),
        "service": cfg.get("service", "openai"),
        "country": cfg.get("country", ""),
        "operator": cfg.get("operator", ""),
    }


def _sms_response(*, msg: Optional[str] = None) -> dict:
    active_cfg = get_sms_config()
    providers = {p: _sms_provider_block(get_sms_provider_config(p)) for p in SMS_PROVIDERS_KNOWN}
    body = {
        "active": active_cfg["provider"],
        "providers": providers,
        # 兼容老前端:顶层暴露 active provider 的字段
        "provider": active_cfg["provider"],
        "api_key_masked": _mask(active_cfg.get("api_key", "")),
        "has_api_key": bool(active_cfg.get("api_key")),
        "service": active_cfg.get("service", "openai"),
        "country": active_cfg.get("country", ""),
        "operator": active_cfg.get("operator", ""),
    }
    if msg:
        body["msg"] = msg
    return body


@router.get("/sms")
def get_sms(_user=Depends(require_user)) -> dict:
    return _sms_response()


@router.put("/sms")
def put_sms(params: SmsParams, _user=Depends(require_user)) -> dict:
    """写入指定 provider 的配置(api_key / service / country / operator)到 sms.<provider>.*。

    api_key 空串视为不更改;set_active=True 时同时把该 provider 设为当前激活。
    """
    provider = (params.provider or "").strip().lower()
    if provider not in SMS_PROVIDERS_KNOWN:
        raise HTTPException(
            400, f"未知 provider: {provider!r} — 支持: {', '.join(SMS_PROVIDERS_KNOWN)}",
        )

    body = params.model_dump(exclude_none=True)
    body.pop("provider", None)
    set_active = body.pop("set_active", False)
    body = _filter_blank_secret(body, ["api_key"])

    # 写到 sms.<provider>.* namespace
    if body:
        ns = {f"{provider}.{k}": v for k, v in body.items()}
        write_setting_group("sms", ns)

    if set_active:
        write_setting_group("sms", {"provider": provider})

    return _sms_response(msg="已保存")


@router.post("/sms/active")
def post_sms_active(params: SmsActiveParams, _user=Depends(require_user)) -> dict:
    """切换当前激活的 SMS provider — 仅改 sms.provider,不改各自配置。"""
    p = (params.provider or "").strip().lower()
    if p not in SMS_PROVIDERS_KNOWN:
        raise HTTPException(
            400, f"未知 provider: {p!r} — 支持: {', '.join(SMS_PROVIDERS_KNOWN)}",
        )
    write_setting_group("sms", {"provider": p})
    return _sms_response(msg=f"已切换到 {p}")


@router.post("/sms/balance")
def post_sms_balance(provider: Optional[str] = None, _user=Depends(require_user)) -> dict:
    """查询余额。不传 provider → 查 active;传则查指定 provider(读它自己的 api_key)。"""
    from autofree.core import sms as sms_mod

    if provider:
        p = provider.strip().lower()
        if p not in SMS_PROVIDERS_KNOWN:
            raise HTTPException(
                400, f"未知 provider: {p!r} — 支持: {', '.join(SMS_PROVIDERS_KNOWN)}",
            )
        cfg = get_sms_provider_config(p)
    else:
        cfg = get_sms_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, f"尚未配置 {cfg['provider']} api_key")
    try:
        prov = sms_mod.get_active_provider(cfg)
        data = prov.get_balance()
    except sms_mod.SmsError as exc:
        raise HTTPException(502, f"{cfg['provider']} 调用失败: {exc}") from exc
    return {
        "provider": data.get("provider"),
        "balance": data.get("balance"),
        "currency": data.get("currency", "USD"),
        "raw": data.get("raw"),
    }


# ─────────────────────────── cpa ───────────────────────────

class CpaParams(BaseModel):
    url: Optional[str] = None
    key: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/cpa")
def get_cpa(_user=Depends(require_user)) -> dict:
    cfg = get_cpa_config()
    return {
        "url": cfg["url"],
        "key_masked": _mask(cfg["key"]),
        "has_key": bool(cfg["key"]),
        "enabled": cfg["enabled"],
    }


# ─────────────────────────── proxy ───────────────────────────

class ProxyParams(BaseModel):
    enabled: Optional[bool] = None
    provider: Optional[str] = None  # iproyal-residential / iproyal-mobile / custom
    host: Optional[str] = None
    port: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    country: Optional[str] = None
    lifetime: Optional[str] = None


def _proxy_response(*, msg: Optional[str] = None) -> dict:
    cfg = get_proxy_config()
    body = {
        "enabled": cfg["enabled"],
        "provider": cfg["provider"],
        "providers_known": list(PROXY_PROVIDERS_KNOWN),
        "provider_defaults": PROXY_PROVIDER_DEFAULTS,
        "host": cfg["host"],
        "port": cfg["port"],
        "username": cfg["username"],
        "password_masked": _mask(cfg["password"]),
        "has_password": bool(cfg["password"]),
        "country": cfg["country"],
        "lifetime": cfg["lifetime"],
    }
    if msg:
        body["msg"] = msg
    return body


@router.get("/proxy")
def get_proxy(_user=Depends(require_user)) -> dict:
    return _proxy_response()


@router.put("/proxy")
def put_proxy(params: ProxyParams, _user=Depends(require_user)) -> dict:
    body = params.model_dump(exclude_none=True)
    if "provider" in body:
        p = body["provider"].strip().lower()
        if p not in PROXY_PROVIDERS_KNOWN:
            raise HTTPException(400, f"未知 provider: {p!r} — 支持: {', '.join(PROXY_PROVIDERS_KNOWN)}")
        body["provider"] = p
    body = _filter_blank_secret(body, ["password"])
    if body:
        write_setting_group("proxy", body)
    return _proxy_response(msg="已保存")


@router.post("/proxy/test")
def post_proxy_test(_user=Depends(require_user)) -> dict:
    """实际通过代理 GET ipinfo.io/json — 验证代理可用并返出口 IP / 城市 / 州。"""
    import urllib.parse

    import httpx

    from autofree.core.browser import get_proxy_options, make_proxy_session_id

    cfg = get_proxy_config()
    if not cfg["enabled"]:
        raise HTTPException(400, "代理未启用 — 请先勾选启用并填写凭证")
    opts = get_proxy_options(session_id=make_proxy_session_id("test"))
    if not opts:
        raise HTTPException(400, "代理配置不全(host / port / 用户名 / 密码 必填)")

    server = opts["server"]
    user = opts["username"]
    pwd = opts["password"]
    user_enc = urllib.parse.quote(user, safe="")
    pwd_enc = urllib.parse.quote(pwd, safe="")
    proxy_url = server.replace("http://", f"http://{user_enc}:{pwd_enc}@", 1)

    logger.info(
        "[proxy/test] server=%s username=%s password_len=%d (params 已挂在密码上)",
        server, user, len(pwd),
    )

    try:
        with httpx.Client(
            proxy=proxy_url, timeout=20.0,
            headers={"User-Agent": "AutoFree/proxy-test"},
        ) as cli:
            resp = cli.get("https://ipinfo.io/json")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ProxyError as exc:
        msg = str(exc)
        if "407" in msg:
            base_pwd = pwd.split('_country-')[0].split('_session-')[0].split('_lifetime-')[0]
            raise HTTPException(
                502,
                "代理认证失败(407)— 检查这几项:\n"
                "  1. IPRoyal 格式是 USERNAME:PASSWORD_params,参数挂在密码末尾(代码已自动追加)\n"
                "  2. 你只需填 IPRoyal 给的最基础 username + 最基础 password,**不要带任何参数后缀**\n"
                "  3. IPRoyal 后台认证方式必须是 Username/Password(不能是 IP Whitelist Only)\n"
                f"\n当前 username = {user}\n"
                f"当前完整 password = {pwd}\n"
                f"基础 password(剥离参数后) = {base_pwd}",
            ) from exc
        raise HTTPException(502, f"代理测试失败: {exc!r}") from exc
    except Exception as exc:
        raise HTTPException(502, f"代理测试失败: {exc!r}") from exc

    return {
        "ok": True,
        "ip": data.get("ip"),
        "country": data.get("country"),
        "region": data.get("region"),
        "city": data.get("city"),
        "org": data.get("org"),
        "timezone": data.get("timezone"),
        "session_user": user,  # 便于在 IPRoyal 后台查日志
        "raw": data,
    }


# ─────────────────────────── cpa (continued) ───────────────────────────


@router.put("/cpa")
def put_cpa(params: CpaParams, _user=Depends(require_user)) -> dict:
    body = {k: v for k, v in params.model_dump(exclude_none=True).items()}
    body = _filter_blank_secret(body, ["key"])
    if body:
        write_setting_group("cpa", body)
    cfg = get_cpa_config()
    return {
        "url": cfg["url"],
        "key_masked": _mask(cfg["key"]),
        "has_key": bool(cfg["key"]),
        "enabled": cfg["enabled"],
        "msg": "已保存",
    }
