"""把 freegen bundle 推到 CPA — autofree 版。

逻辑:
  - 不再调 autoteam 的 _save_normalized_auth_file(那是 codex-<email>-<plan>-<hash>.json
    的 autoteam-style 命名)。autofree 走自己的 storage.write_auth_json,
    输出 output/auth/<email>.json,直接拿这个文件上传给 CPA 即可。
  - CPA 未配 / disabled → silently 跳过(返 ok=True 表"无需做")。

返:
  (ok, message, file_path | None)
  - ok=True 包含"已推 CPA"和"CPA 未启用所以跳过"两种正常路径
  - ok=False 表示真出错(网络/4xx/5xx)
"""

from __future__ import annotations

import logging
from pathlib import Path

from autofree.core.cpa_sync import is_cpa_configured, upload_to_cpa

logger = logging.getLogger(__name__)


def push_auth_file(json_path: str | Path) -> tuple[bool, str]:
    """把已落盘的 auth JSON 推到 CPA。

    json_path:storage.write_auth_json 写出的 output/auth/<email>.json。
    """
    p = Path(json_path)
    if not p.exists():
        return False, f"文件不存在: {p}"

    if not is_cpa_configured():
        logger.info("[cpa_push] CPA 未启用,跳过 file=%s", p.name)
        return True, "CPA 未启用,已跳过"

    return upload_to_cpa(p)
