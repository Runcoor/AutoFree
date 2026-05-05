"""dreamhunter2333/cloudflare_temp_email 后端的最小 HTTP 客户端。

只覆盖 freegen 用得到的 4 个动作:
- 创建临时邮箱 (POST /admin/new_address)
- 查收件 (GET /admin/mails?address=)
- 删除邮箱 (DELETE /admin/delete_address/{id})
- 提取 6 位 OTP

不依赖 autoteam.mail.* 任何模块。MIME 解析用 stdlib email。
"""

from __future__ import annotations

import email as email_pkg
import html as html_lib
import logging
import re
import time
from email.header import decode_header, make_header
from typing import Any

import requests

from autofree.core.config import EMAIL_POLL_INTERVAL, EMAIL_POLL_TIMEOUT, get_mail_config
from autofree.core.identity import random_email_prefix

logger = logging.getLogger(__name__)


_OTP_PATTERNS = (
    r"(?:temporary\s+(?:openai|chatgpt)\s+login\s+code(?:\s+is)?|verification\s+code(?:\s+is)?|login\s+code(?:\s+is)?|code(?:\s+is)?|验证码(?:为|是)?)\D{0,24}(\d{6})",
    r"\b(\d{6})\b",
)


class MailError(Exception):
    pass


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _html_to_text(value: Any) -> str:
    s = str(value or "")
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(?:p|div|tr|table|h[1-6]|li|td|section|article)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[\t\r\f\v ]+", " ", s)
    s = re.sub(r"\n\s+", "\n", s)
    return s.strip()


def _parse_mime(raw: str | None) -> dict:
    """raw MIME → {subject, text, html, from, to}."""
    if not raw:
        return {"subject": "", "text": "", "html": "", "from": "", "to": ""}
    try:
        msg = email_pkg.message_from_string(raw)
    except Exception:
        return {"subject": "", "text": raw, "html": "", "from": "", "to": ""}

    subject = _decode_header(msg.get("Subject", ""))
    from_addr = _decode_header(msg.get("From", ""))
    to_addr = _decode_header(msg.get("To", ""))

    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            try:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace") if payload else ""
            except Exception:
                decoded = ""
            if ctype == "text/plain" and not text_body:
                text_body = decoded
            elif ctype == "text/html" and not html_body:
                html_body = decoded
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            decoded = ""
        if msg.get_content_type() == "text/html":
            html_body = decoded
        else:
            text_body = decoded

    return {"subject": subject, "text": text_body, "html": html_body, "from": from_addr, "to": to_addr}


class MailClient:
    """cf_temp_email 最小客户端。"""

    def __init__(self, base_url: str | None = None, admin_password: str | None = None):
        cfg = get_mail_config()
        self.base_url = (base_url or cfg["base_url"]).rstrip("/")
        self.admin_password = admin_password or cfg["password"]
        if not self.base_url or not self.admin_password:
            raise MailError("MailClient 缺少 base_url / admin_password — 检查环境变量")
        self.session = requests.Session()

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "x-admin-auth": self.admin_password}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def login(self) -> None:
        r = self.session.get(self._url("/admin/address"), headers=self._headers(), params={"limit": 1, "offset": 0}, timeout=30)
        if r.status_code in (401, 403):
            raise MailError(f"鉴权失败 HTTP {r.status_code} — 检查 admin password")
        if r.status_code != 200:
            raise MailError(f"登录失败 HTTP {r.status_code}: {(r.text or '')[:200]}")
        try:
            payload = r.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or "results" not in payload:
            raise MailError(
                "服务器响应不像 cf_temp_email(缺 `results` 字段)。base_url 可能指向 maillab,"
                "PoC 暂只支持 cf_temp_email。"
            )
        logger.info("[mail] 登录通过 base=%s", self.base_url)

    def create_email(self, domain: str, prefix: str | None = None) -> tuple[int, str]:
        """返回 (address_id, email_address)。domain 不带 @。"""
        domain = domain.lstrip("@").strip()
        if not domain:
            raise MailError("create_email: domain 为空")
        clean_prefix = (prefix or random_email_prefix()).lower()
        clean_prefix = re.sub(r"[^a-z0-9._]", "", clean_prefix)[:60] or random_email_prefix()
        r = self.session.post(
            self._url("/admin/new_address"),
            headers=self._headers(),
            json={"name": clean_prefix, "domain": domain, "enablePrefix": False},
            timeout=30,
        )
        if r.status_code != 200:
            raise MailError(f"create_email HTTP {r.status_code}: {(r.text or '')[:200]}")
        try:
            data = r.json()
        except Exception:
            data = {}
        if not isinstance(data, dict) or "address" not in data:
            raise MailError(f"create_email 响应缺 address 字段: {data!r}")
        address = data["address"]
        address_id = data.get("address_id")
        if not address_id:
            # fallback: 按名查 id
            try:
                listed = self.session.get(
                    self._url("/admin/address"),
                    headers=self._headers(),
                    params={"limit": 1, "offset": 0, "query": address},
                    timeout=30,
                )
                results = (listed.json() or {}).get("results") or []
                if results:
                    address_id = results[0].get("id")
            except Exception:
                pass
        logger.info("[mail] 创建邮箱: %s (id=%s)", address, address_id)
        return address_id, address

    def delete_email(self, address_id: int | None) -> None:
        if not address_id:
            return
        try:
            self.session.delete(self._url(f"/admin/delete_address/{address_id}"), headers=self._headers(), timeout=30)
        except Exception as exc:
            logger.warning("[mail] 删除邮箱 id=%s 失败(忽略): %s", address_id, exc)

    def list_mails(self, to_email: str, size: int = 10) -> list[dict]:
        """返回最新邮件列表;每项: {id, subject, text, html, from, raw}。"""
        r = self.session.get(
            self._url("/admin/mails"),
            headers=self._headers(),
            params={"limit": size, "offset": 0, "address": to_email.strip().lower()},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        try:
            data = r.json() or {}
        except Exception:
            return []
        out = []
        for row in data.get("results", []):
            parsed = _parse_mime(row.get("raw") or "")
            out.append({
                "id": row.get("id"),
                "subject": parsed["subject"],
                "text": parsed["text"],
                "html": parsed["html"],
                "from": parsed["from"] or row.get("source") or "",
                "raw": row.get("raw") or "",
            })
        return out

    def extract_otp(self, mail: dict) -> str | None:
        """从单封邮件正文中提取 6 位验证码。"""
        sources = []
        if mail.get("text"):
            sources.append(mail["text"])
        if mail.get("html"):
            visible = _html_to_text(mail["html"])
            if visible:
                sources.append(visible)
        for s in sources:
            for pat in _OTP_PATTERNS:
                m = re.search(pat, s, re.IGNORECASE)
                if m:
                    return m.group(1)
        return None

    def wait_for_otp(self, to_email: str, *, after_id: int = 0, timeout: int | None = None, sender_keyword: str | None = None) -> tuple[int, str]:
        """轮询等待 OTP 邮件。返回 (mail_id, otp_code)。超时抛 TimeoutError。

        after_id: 只接受 id 严格大于该值的邮件,跳过登录前的旧邮件。
        sender_keyword: 可选,限定发件人含关键字(如 "openai")。
        """
        timeout = timeout or EMAIL_POLL_TIMEOUT
        start = time.time()
        last_log = 0.0
        while time.time() - start < timeout:
            try:
                mails = self.list_mails(to_email, size=10)
            except Exception as exc:
                logger.warning("[mail] 轮询异常,重试: %s", exc)
                mails = []
            for m in mails:
                mid = m.get("id") or 0
                if mid <= after_id:
                    continue
                if sender_keyword and sender_keyword.lower() not in (m.get("from") or "").lower():
                    continue
                code = self.extract_otp(m)
                if code:
                    logger.info("[mail] OTP 命中 id=%s subject=%r code=%s", mid, m.get("subject", "")[:40], code)
                    return mid, code
            now = time.time()
            if now - last_log > 5:
                elapsed = int(now - start)
                logger.info("[mail] 等待 OTP... (%ds)", elapsed)
                last_log = now
            time.sleep(EMAIL_POLL_INTERVAL)
        raise TimeoutError(f"等待 {to_email} 的 OTP 超时 ({timeout}s)")

    def latest_mail_id(self, to_email: str) -> int:
        """返回当前收件箱最新一封邮件的 id;空收件箱返回 0。用于建 OTP after_id baseline。"""
        try:
            mails = self.list_mails(to_email, size=1)
            return (mails[0].get("id") or 0) if mails else 0
        except Exception:
            return 0
