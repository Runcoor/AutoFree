"""freegen 流程的协作式中断信号。

进程级单 batch (autoteam._playwright_lock 已串行化),module-level Event 够用。
- API `POST /api/freegen/stop` 调 request_stop()
- batch.run_batch 启动时 reset_stop(),账号之间检查 is_stop_requested()
- oauth._solve_phone_gate 在重试间隙 + 内层 SMS 轮询里检查 → 立即 cancel/ban order 退款
- 检测到中断的代码抛 BatchStopped(走 batch.py 失败路径,记录 error_kind="stopped")
"""

from __future__ import annotations

import threading

_STOP_EVENT = threading.Event()


def request_stop() -> None:
    _STOP_EVENT.set()


def reset_stop() -> None:
    _STOP_EVENT.clear()


def is_stop_requested() -> bool:
    return _STOP_EVENT.is_set()
