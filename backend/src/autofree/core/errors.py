"""freegen 共用异常。"""

from __future__ import annotations


class FreegenError(Exception):
    """freegen 流程错误基类。"""


class RegisterBlocked(FreegenError):
    """注册被风控阻断:add-phone / duplicate / 其它 terminal 错误。"""

    def __init__(self, step: str, reason: str, *, is_phone: bool = False, is_duplicate: bool = False):
        super().__init__(f"[{step}] {reason}")
        self.step = step
        self.reason = reason
        self.is_phone = is_phone
        self.is_duplicate = is_duplicate


class RegisterFailed(FreegenError):
    """注册流程在某步失败但不是终结性的(比如点击没生效、页面没推进)。"""


class OAuthFailed(FreegenError):
    """OAuth 拿不到 auth_code 或 token 交换失败。"""


class BatchStopped(FreegenError):
    """用户从 UI/API 主动停止了 batch — 当前账号会被记为 stopped,batch 立即退出。"""
