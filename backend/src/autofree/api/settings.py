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

from autofree.core.config import get_cpa_config, get_mail_config, get_sms_config, write_setting_group
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
    provider: Optional[str] = None
    api_key: Optional[str] = None
    service: Optional[str] = None
    country: Optional[str] = None
    operator: Optional[str] = None


def _sms_response(cfg: dict, *, msg: Optional[str] = None) -> dict:
    body = {
        "provider": cfg.get("provider", "5sim"),
        "api_key_masked": _mask(cfg.get("api_key", "")),
        "has_api_key": bool(cfg.get("api_key")),
        "service": cfg.get("service", "openai"),
        "country": cfg.get("country", ""),
        "operator": cfg.get("operator", ""),
    }
    if msg:
        body["msg"] = msg
    return body


@router.get("/sms")
def get_sms(_user=Depends(require_user)) -> dict:
    return _sms_response(get_sms_config())


@router.put("/sms")
def put_sms(params: SmsParams, _user=Depends(require_user)) -> dict:
    body = {k: v for k, v in params.model_dump(exclude_none=True).items()}
    body = _filter_blank_secret(body, ["api_key"])
    if body:
        write_setting_group("sms", body)
    return _sms_response(get_sms_config(), msg="已保存")


@router.post("/sms/balance")
def post_sms_balance(_user=Depends(require_user)) -> dict:
    from autofree.core import sms as sms_mod

    cfg = get_sms_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, f"尚未配置 {cfg['provider']} api_key")
    try:
        provider = sms_mod.get_active_provider(cfg)
        data = provider.get_balance()
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
