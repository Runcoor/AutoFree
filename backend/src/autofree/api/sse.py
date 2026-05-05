"""SSE 端点 — 流式推 freegen 任务的 stage / events。

实现:轮询 freegen.api.freegen._current 状态,把新事件以 EventSource 格式 yield 给客户端。
任务进入 finished/stopped/failed 后,推一个 close 事件然后 break。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from autofree.api import freegen as freegen_api
from autofree.deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/task/{task_id}")
async def stream_task(task_id: str, request: Request, _user=Depends(require_user)):
    """订阅任务事件流。任务不存在/已结束 → 推一个 snapshot 然后关闭。"""

    async def event_gen():
        last_idx = 0
        last_send = 0.0
        while True:
            if await request.is_disconnected():
                break

            state = freegen_api._current
            if not state or state.get("task_id") != task_id:
                yield _sse("snapshot", {"error": "no_such_task"})
                yield _sse("close", {})
                break

            events = state.get("events", [])
            # 只推新事件
            if last_idx < len(events):
                for ev in events[last_idx:]:
                    yield _sse(ev.get("stage", "event"), ev)
                last_idx = len(events)

            # 心跳 ≤ 15s 保持连接
            now = time.time()
            if now - last_send > 15:
                yield _sse("ping", {"ts": now})
                last_send = now

            stage = state.get("stage", "")
            if stage in ("finished", "stopped", "failed"):
                # 已结束:再推最后一次 snapshot 然后 close
                yield _sse("snapshot", freegen_api._snapshot_running())
                yield _sse("close", {"final": stage})
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
