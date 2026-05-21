"""手机号注册国家码表 — SMS provider country slug 与 ISO/dial code 的双向映射。

用途:
  - 注册时给 chatgpt.com 「选择国家」下拉填 ISO 码(如 GB / FR)
  - 输入手机号时按 dial code 截取本地号部分(如 +44 78xxxx → 78xxxx)
  - SMS 5sim/hero-sms 配置里的 country 字段是 slug("france" / "england"),需翻译到 ISO

数据 port 自 src/phoneCountryCatalog.js(JS 参考实现),只保留 5sim / hero-sms 可能用到的
+ 主流国家。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhoneCountry:
    """手机号注册用的国家信息。

    iso_code: 2 字母 ISO 码(如 'GB'),用于 chatgpt.com React Aria Select 的 data-key
    dial_code: 国家拨号码(如 '44'),用于解析 +44xxx 本地号
    cn_name: 中文显示名(如 '英国'),fallback 文案匹配
    en_aliases: 英文别名 list,fallback 文案匹配
    sms_slugs: SMS provider 可能使用的 slug list(5sim/hero-sms 都用 slug,如 'england')
    """

    iso_code: str
    dial_code: str
    cn_name: str
    en_aliases: tuple[str, ...] = ()
    sms_slugs: tuple[str, ...] = ()


# 主表 — 保留 5sim/hero-sms 实际可用的国家
_COUNTRIES: tuple[PhoneCountry, ...] = (
    PhoneCountry("GB", "44", "英国",
                 ("United Kingdom", "UK", "Britain", "Great Britain", "England"),
                 ("england", "uk", "britain", "gb", "unitedkingdom")),
    PhoneCountry("US", "1", "美国",
                 ("United States", "USA", "America"),
                 ("usa", "us", "america", "unitedstates", "usapremium")),
    PhoneCountry("CA", "1", "加拿大", ("Canada",), ("canada", "ca")),
    PhoneCountry("FR", "33", "法国", ("France",), ("france", "fr")),
    PhoneCountry("DE", "49", "德国", ("Germany", "Deutschland"), ("germany", "de")),
    PhoneCountry("ES", "34", "西班牙", ("Spain",), ("spain", "es")),
    PhoneCountry("IT", "39", "意大利", ("Italy",), ("italy", "it")),
    PhoneCountry("NL", "31", "荷兰", ("Netherlands", "Holland"), ("netherlands", "nl")),
    PhoneCountry("BE", "32", "比利时", ("Belgium",), ("belgium", "be")),
    PhoneCountry("AT", "43", "奥地利", ("Austria",), ("austria", "at")),
    PhoneCountry("CH", "41", "瑞士", ("Switzerland",), ("switzerland", "ch")),
    PhoneCountry("SE", "46", "瑞典", ("Sweden",), ("sweden", "se")),
    PhoneCountry("NO", "47", "挪威", ("Norway",), ("norway", "no")),
    PhoneCountry("DK", "45", "丹麦", ("Denmark",), ("denmark", "dk")),
    PhoneCountry("FI", "358", "芬兰", ("Finland",), ("finland", "fi")),
    PhoneCountry("PL", "48", "波兰", ("Poland",), ("poland", "pl")),
    PhoneCountry("PT", "351", "葡萄牙", ("Portugal",), ("portugal", "pt")),
    PhoneCountry("IE", "353", "爱尔兰", ("Ireland",), ("ireland", "ie")),
    PhoneCountry("CZ", "420", "捷克", ("Czech Republic", "Czechia"), ("czech", "cz")),
    PhoneCountry("GR", "30", "希腊", ("Greece",), ("greece", "gr")),
    PhoneCountry("RO", "40", "罗马尼亚", ("Romania",), ("romania", "ro")),
    PhoneCountry("HU", "36", "匈牙利", ("Hungary",), ("hungary", "hu")),
    PhoneCountry("TR", "90", "土耳其", ("Turkey", "Turkiye"), ("turkey", "tr")),
    PhoneCountry("IL", "972", "以色列", ("Israel",), ("israel", "il")),
    PhoneCountry("AE", "971", "阿联酋", ("UAE", "United Arab Emirates"), ("uae", "ae")),
    PhoneCountry("SA", "966", "沙特阿拉伯", ("Saudi Arabia",), ("saudiarabia", "sa")),
    PhoneCountry("SG", "65", "新加坡", ("Singapore",), ("singapore", "sg")),
    PhoneCountry("MY", "60", "马来西亚", ("Malaysia",), ("malaysia", "my")),
    PhoneCountry("TH", "66", "泰国", ("Thailand",), ("thailand", "th")),
    PhoneCountry("VN", "84", "越南", ("Vietnam",), ("vietnam", "vn")),
    PhoneCountry("PH", "63", "菲律宾", ("Philippines",), ("philippines", "ph")),
    PhoneCountry("ID", "62", "印度尼西亚", ("Indonesia",), ("indonesia", "id")),
    PhoneCountry("IN", "91", "印度", ("India",), ("india", "in")),
    PhoneCountry("AU", "61", "澳大利亚", ("Australia",), ("australia", "au")),
    PhoneCountry("NZ", "64", "新西兰", ("New Zealand",), ("newzealand", "nz")),
    PhoneCountry("BR", "55", "巴西", ("Brazil",), ("brazil", "br")),
    PhoneCountry("MX", "52", "墨西哥", ("Mexico",), ("mexico", "mx")),
    PhoneCountry("AR", "54", "阿根廷", ("Argentina",), ("argentina", "ar")),
    PhoneCountry("ZA", "27", "南非", ("South Africa",), ("southafrica", "za")),
    PhoneCountry("EG", "20", "埃及", ("Egypt",), ("egypt", "eg")),
    PhoneCountry("NG", "234", "尼日利亚", ("Nigeria",), ("nigeria", "ng")),
    PhoneCountry("PK", "92", "巴基斯坦", ("Pakistan",), ("pakistan", "pk")),
    PhoneCountry("UA", "380", "乌克兰", ("Ukraine",), ("ukraine", "ua")),
    PhoneCountry("RU", "7", "俄罗斯", ("Russia",), ("russia", "ru")),
    PhoneCountry("KZ", "7", "哈萨克斯坦", ("Kazakhstan",), ("kazakhstan", "kz")),
    PhoneCountry("KE", "254", "肯尼亚", ("Kenya",), ("kenya", "ke")),
)


# 索引(全部小写匹配)
_BY_ISO: dict[str, PhoneCountry] = {c.iso_code.lower(): c for c in _COUNTRIES}
_BY_SLUG: dict[str, PhoneCountry] = {}
for _c in _COUNTRIES:
    for _s in _c.sms_slugs:
        _BY_SLUG[_s.lower()] = _c
    _BY_SLUG[_c.iso_code.lower()] = _c
    _BY_SLUG[_c.cn_name] = _c
    for _alias in _c.en_aliases:
        _BY_SLUG[_alias.lower().replace(" ", "")] = _c


DEFAULT = _BY_ISO["gb"]  # 用户默认 — 跟 JS 参考 phoneCountryCode='GB' 一致


def from_sms_slug(slug: str) -> PhoneCountry:
    """SMS provider 配置里的 country 字段 → PhoneCountry。

    支持: "england" / "france" / "GB" / "fr" / "United Kingdom" / "英国" / 空字符串(→ DEFAULT)。
    完全匹配不到时返 DEFAULT(GB),不抛错 — 保证流程能继续跑。
    """
    if not slug:
        return DEFAULT
    key = str(slug).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if not key:
        return DEFAULT
    if key in _BY_SLUG:
        return _BY_SLUG[key]
    # 纯数字尝试当 dial code 反查(如 "44" → GB)
    if key.isdigit():
        for c in _COUNTRIES:
            if c.dial_code == key:
                return c
    return DEFAULT


def from_iso(iso: str) -> PhoneCountry:
    """ISO 码反查。找不到返 DEFAULT。"""
    if not iso:
        return DEFAULT
    return _BY_ISO.get(iso.strip().lower(), DEFAULT)


def strip_dial_prefix(phone_e164: str, country: PhoneCountry) -> str:
    """+44 7912345678 → 7912345678(去掉 + 和 dial code)。

    若不匹配 country 的 dial code,只去 +,保留全部数字 — 让 OpenAI 自动识别。
    """
    s = (phone_e164 or "").strip()
    if not s:
        return ""
    if s.startswith("+"):
        s = s[1:]
    if country and country.dial_code and s.startswith(country.dial_code):
        return s[len(country.dial_code):]
    return s


def all_countries() -> list[PhoneCountry]:
    """全表 — 给 UI 国家选择器用(我们目前不用,SMS country 一统天下)。"""
    return list(_COUNTRIES)
