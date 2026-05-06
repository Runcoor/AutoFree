"""core 模块运行时配置 — 从 DB Setting 表读 cloud-mail / SMS / CPA 配置。

env 只剩系统级(数据库 URL、应用密码、输出目录),业务配置走 DB Setting 表,
web 改了立即生效。

模块级常量(OUTPUT_DIR / SCREENSHOT_DIR / EMAIL_POLL_*)从 autofree.settings 取。
DB 配置(get_mail_config / get_sms_config / get_cpa_config)走函数,每次现读现用,
保证 web 改了配置立即生效。
"""

from __future__ import annotations

import json

from autofree.settings import get_settings

_settings = get_settings()

OUTPUT_DIR = _settings.output_dir
SCREENSHOT_DIR = _settings.screenshot_dir

# 邮件轮询参数 — 暂保留为常量,以后真要 web 配再升级
EMAIL_POLL_INTERVAL = 3
EMAIL_POLL_TIMEOUT = 180


# ─── DB-backed 配置读取 ─────────────────────────────────────────────────────

def _read_setting(key: str, default: str = "") -> str:
    """从 DB Setting 表读单个 key;不存在或异常返默认值。

    本地循环导入:core/config 在很多注册流程入口被 import,而 db 模块依赖于 settings,
    settings 依赖于 core/config — 通过函数内 import 打破。
    """
    try:
        from sqlalchemy import select

        from autofree.db.base import SessionLocal
        from autofree.db.models import Setting

        with SessionLocal() as db:
            row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
            return row.value if row else default
    except Exception:
        return default


def _read_setting_group(prefix: str) -> dict:
    """读所有以 prefix. 开头的 settings,返 dict(去掉前缀)。"""
    try:
        from sqlalchemy import select

        from autofree.db.base import SessionLocal
        from autofree.db.models import Setting

        with SessionLocal() as db:
            rows = db.execute(select(Setting).where(Setting.key.like(f"{prefix}.%"))).scalars().all()
            return {r.key.removeprefix(f"{prefix}."): r.value for r in rows}
    except Exception:
        return {}


def get_mail_config() -> dict:
    g = _read_setting_group("cloud_mail")
    return {
        "base_url": (g.get("base_url") or "").rstrip("/"),
        "password": g.get("password") or "",
    }


SMS_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "5sim": {"service": "openai", "country": "france", "operator": "any"},
    "hero-sms": {"service": "openai", "country": "england", "operator": "any"},
}
SMS_PROVIDERS_KNOWN = tuple(SMS_PROVIDER_DEFAULTS.keys())


def _sms_defaults(provider: str) -> dict[str, str]:
    return SMS_PROVIDER_DEFAULTS.get(provider, SMS_PROVIDER_DEFAULTS["5sim"])


def get_sms_config() -> dict:
    """返回 active provider 的完整配置(oauth / phone gate 调用方用)。

    配置存储:
      - sms.provider          — 当前激活的 provider(5sim / hero-sms)
      - sms.<provider>.api_key / .service / .country / .operator — 每个 provider 独立 namespace
      - sms.api_key / .service / ...(legacy 扁平字段) — 向后兼容,空则才回退到此
    """
    g = _read_setting_group("sms")
    provider = (g.get("provider") or "5sim").strip().lower() or "5sim"
    defaults = _sms_defaults(provider)

    def _ns(key: str, default: str) -> str:
        # 优先 namespace 字段;空则回退 legacy 扁平(为旧用户向后兼容)
        return (g.get(f"{provider}.{key}") or g.get(key) or default).strip() or default

    return {
        "provider": provider,
        "api_key": (g.get(f"{provider}.api_key") or g.get("api_key") or "").strip(),
        "service": _ns("service", defaults["service"]),
        "country": _ns("country", defaults["country"]),
        "operator": _ns("operator", defaults["operator"]),
    }


def get_sms_provider_config(provider: str) -> dict:
    """返回指定 provider 的配置(供 settings 页同时编辑多个 provider 的 form 用)。"""
    g = _read_setting_group("sms")
    p = (provider or "").strip().lower()
    defaults = _sms_defaults(p)
    active = (g.get("provider") or "5sim").strip().lower()

    def _ns(key: str, default: str) -> str:
        # legacy fallback 只对 active provider 生效,避免 5sim 旧 api_key 被 hero-sms tab 读到
        legacy = g.get(key, "") if p == active else ""
        return (g.get(f"{p}.{key}") or legacy or default).strip() or default

    return {
        "provider": p,
        "api_key": (g.get(f"{p}.api_key") or (g.get("api_key", "") if p == active else "") or "").strip(),
        "service": _ns("service", defaults["service"]),
        "country": _ns("country", defaults["country"]),
        "operator": _ns("operator", defaults["operator"]),
    }


def get_cpa_config() -> dict:
    g = _read_setting_group("cpa")
    enabled_raw = (g.get("enabled") or "").strip().lower()
    return {
        "url": (g.get("url") or "").rstrip("/"),
        "key": g.get("key") or "",
        "enabled": enabled_raw in ("1", "true", "yes", "on"),
    }


def assert_configured() -> None:
    """注册启动前调,缺关键配置直接抛 RuntimeError。"""
    mail = get_mail_config()
    missing = []
    if not mail["base_url"]:
        missing.append("cloud_mail.base_url")
    if not mail["password"]:
        missing.append("cloud_mail.password")
    sms = get_sms_config()
    if not sms["api_key"]:
        missing.append(f"sms.api_key ({sms['provider']})")
    if missing:
        raise RuntimeError("缺少配置(请到设置页填): " + ", ".join(missing))


# ─── 备用:把 dict 序列化保存到 Setting 表(供 api 层使用)──────────────────────

def write_setting(key: str, value: str) -> None:
    from sqlalchemy import select

    from autofree.db.base import SessionLocal
    from autofree.db.models import Setting

    with SessionLocal() as db:
        row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))
        db.commit()


def write_setting_group(prefix: str, mapping: dict) -> None:
    """批量写一组 settings(已存在的覆盖,新的插入)。"""
    for k, v in mapping.items():
        full_key = f"{prefix}.{k}"
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            v = "true" if v else "false"
        else:
            v = "" if v is None else str(v)
        write_setting(full_key, v)
