"""统一轨迹追踪 API 路由。

提供事件查询、会话谱系、统计投影等接口，挂载到 LangGraph API 的 custom_app 中。

路由：
- GET  /api/traces/threads/{thread_id}/events   — 事件列表
- GET  /api/traces/threads/{thread_id}/lineage  — 会话谱系树
- GET  /api/traces/threads/{thread_id}/stats    — 会话统计（投影）
- GET  /api/traces/tasks/{task_id}/events       — 子任务事件（跨 thread）
- GET  /api/traces/threads/{thread_id}/llm-calls — LLM 调用清单
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from agent.trace.event_store import EventStore
from agent.trace.session_lineage import SessionLineageEngine
from agent.workspace_manager import get_workspace_manager

_logger = logging.getLogger(__name__)

# ── 单例 EventStore（延迟初始化，trace 全局共享） ──────────────
_store: Optional[EventStore] = None


def _get_store() -> EventStore:
    """获取全局共享的 EventStore 实例（不随工作区切换）。

    2026-08-27 决策：traces.sqlite 全局共享，切换工作区不丢历史 trace。
    """
    global _store
    wm = get_workspace_manager()
    db_path = str(wm.shared_trace_db)

    if _store is None:
        _store = EventStore(db_path)
        _store.open()
        _logger.info("[trace_api] store opened: %s", db_path)

    return _store


def _get_lineage_engine() -> SessionLineageEngine:
    return SessionLineageEngine(_get_store())


# ── 辅助 ───────────────────────────────────────────────────────

def _parse_pagination(request: Request) -> tuple[int, int]:
    """解析分页参数。"""
    try:
        limit = int(request.query_params.get("limit", "200"))
    except (ValueError, TypeError):
        limit = 200
    try:
        offset = int(request.query_params.get("offset", "0"))
    except (ValueError, TypeError):
        offset = 0
    return min(limit, 500), max(offset, 0)


# ── 路由处理函数 ───────────────────────────────────────────────

async def list_events(request: Request) -> JSONResponse:
    """列出事件。

    Query params:
        thread_id (path): 线程 ID
        task_id: 可选，按子任务过滤
        event_type: 可选，按事件类型过滤
        seq_from: 可选，起始 seq
        seq_to: 可选，结束 seq
        limit: 默认 200，最大 500
        offset: 默认 0
    """
    thread_id = request.path_params.get("thread_id", "")
    if not thread_id:
        return JSONResponse({"error": "thread_id required"}, status_code=400)

    store = _get_store()
    limit, offset = _parse_pagination(request)

    task_id = request.query_params.get("task_id", "")
    event_type = request.query_params.get("event_type", "")
    seq_from = request.query_params.get("seq_from", "0")
    seq_to = request.query_params.get("seq_to", "0")

    events = store.query_events(
        thread_id=thread_id,
        task_id=task_id or None,
        event_type=event_type or None,
        seq_from=int(seq_from) if seq_from else 0,
        seq_to=int(seq_to) if seq_to else 0,
        limit=limit,
        offset=offset,
    )
    total = store.count_events(thread_id)

    return JSONResponse({
        "thread_id": thread_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(events),
        "events": events,
    })


async def get_lineage(request: Request) -> JSONResponse:
    """获取会话谱系树。

    Returns:
        {
            "thread_id": ...,
            "ancestors": [...],
            "node": {...},
            "descendants": [{..., "children": [...]}, ...]
        }
    """
    thread_id = request.path_params.get("thread_id", "")
    if not thread_id:
        return JSONResponse({"error": "thread_id required"}, status_code=400)

    engine = _get_lineage_engine()
    tree = engine.trace_session(thread_id)
    return JSONResponse(tree)


async def get_stats(request: Request) -> JSONResponse:
    """获取会话统计（基于事件日志投影）。

    Returns:
        {thread_id, total_llm_ms, total_input_tokens, total_output_tokens,
         total_cache_read_tokens, total_reasoning_tokens, step_count,
         tool_call_count, subagent_count}
    """
    thread_id = request.path_params.get("thread_id", "")
    if not thread_id:
        return JSONResponse({"error": "thread_id required"}, status_code=400)

    store = _get_store()
    stats = store.compute_session_stats(thread_id)
    return JSONResponse(stats)


async def get_llm_calls(request: Request) -> JSONResponse:
    """获取某 thread 的所有 LLM 调用详情。"""
    thread_id = request.path_params.get("thread_id", "")
    if not thread_id:
        return JSONResponse({"error": "thread_id required"}, status_code=400)

    store = _get_store()
    calls = store.get_llm_calls(thread_id)
    return JSONResponse({
        "thread_id": thread_id,
        "count": len(calls),
        "calls": calls,
    })


async def get_task_events(request: Request) -> JSONResponse:
    """获取某个子任务的所有事件（跨 thread）。"""
    task_id = request.path_params.get("task_id", "")
    if not task_id:
        return JSONResponse({"error": "task_id required"}, status_code=400)

    store = _get_store()
    events = store.get_task_events(task_id)
    return JSONResponse({
        "task_id": task_id,
        "count": len(events),
        "events": events,
    })


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/traces/threads/{thread_id}/events", list_events, methods=["GET"]),
    Route("/api/traces/threads/{thread_id}/lineage", get_lineage, methods=["GET"]),
    Route("/api/traces/threads/{thread_id}/stats", get_stats, methods=["GET"]),
    Route("/api/traces/threads/{thread_id}/llm-calls", get_llm_calls, methods=["GET"]),
    Route("/api/traces/tasks/{task_id}/events", get_task_events, methods=["GET"]),
]