"""TraceRecorderMiddleware — 统一事件采集。

在每次 LLM 调用和 Tool 调用时自动记录事件到 EventStore（持久化），
作为本地自包含的追踪日志（生产环境可无 LangSmith / Langfuse，仍可回溯）。

Token 计量由 TokenMeterMiddleware 独立负责（写入 state.token_stats）：
本中间件只记事件、不写 state.token_stats，避免两处同时 update 同一字段
导致 reducer 累加重复（实测双中间件时 token 统计翻倍）。

安装方式（在 middleware 链的最外层）：
    TraceRecorderMiddleware(db_path=".../traces.sqlite", agent_type="chat_agent")

事件采集：
- wrap_model_call / awrap_model_call: LLM 调用开始/结束（含 token 用量入事件）
- wrap_tool_call / awrap_tool_call: 工具调用开始/结束 + start_async_task 检测
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, TypeVar

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)

from agent.trace.event_log import EventType, TraceEvent
from agent.trace.event_store import get_event_store

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")


class TraceRecorderMiddleware(AgentMiddleware):
    """统一事件采集 Middleware。

    采集 event 写入 EventStore（持久化，本地自包含追踪日志）。
    Token 计量由 TokenMeterMiddleware 负责，本中间件不写 state.token_stats，
    避免与 TokenMeter 双写导致累加重复。
    """

    def __init__(self, db_path: str, agent_type: str = "chat_agent"):
        # 共享实例（同路径进程内唯一）：中间件本身也是进程内单例，但 trace API、
        # sync 循环同样打开这份 traces.sqlite —— 各建一条连接会 fd 泄漏 + seq 撞号。
        self._store = get_event_store(db_path)
        self._agent_type = agent_type
        _logger.info(
            "[TraceRecorder] initialized: agent=%s db=%s", agent_type, db_path
        )

    # ── thread_id 获取 ──────────────────────────────────────────

    def _get_thread_id(self, request: Any) -> str:
        """取「当前 run **自己**的 thread id」（模型调用与工具调用共用）。

        来源优先级：
          1. `request.runtime.execution_info.thread_id` —— run 自己的线程。
             `ModelRequest` 与 `ToolCallRequest` 都带 runtime/execution_info，
             所以两条路径能用同一个来源（这是本中间件的关键约束：模型事件与
             工具事件必须落在同一个 thread 上）。
          2. `configurable.thread_id` —— 兜底（execution_info 缺失/为空时）。

        ⚠️ 曾经的做法是在 `wrap_model_call` 里把结果存到 `self._cached_thread_id`，
        再由 `wrap_tool_call` 读回来。**那是并发下必错的写法**：中间件实例是进程内
        单例，而「写入」（模型调用）到「读取」（工具调用）之间隔着**整个 LLM 推理
        时长**，期间别的 run 的模型调用会覆盖这个属性 → 工具事件系统性挂到最后一个
        发起模型调用的会话上（生产 10 个 run 共用 1 个实例，必有此现象）。
        现取（contextvar / request 携带）才与并发数无关。
        """
        try:
            runtime = getattr(request, "runtime", None)
            if runtime is not None:
                exec_info = getattr(runtime, "execution_info", None)
                if exec_info is not None:
                    tid = getattr(exec_info, "thread_id", "")
                    if tid:
                        return str(tid)
        except Exception:
            pass
        try:
            from langgraph.config import get_config as _cfg
            if _cfg is not None:
                cfg = _cfg() or {}
                tid = (cfg.get("configurable") or {}).get("thread_id", "")
                if tid:
                    return str(tid)
        except Exception:
            pass
        return ""

    def _get_parent_thread_id(self) -> str:
        """从 configurable 中读取 trace_parent_thread_id。"""
        try:
            from langgraph.config import get_config as _cfg
            if _cfg is not None:
                cfg = _cfg()
                if cfg is not None:
                    return (
                        (cfg.get("configurable") or {})
                        .get("trace_parent_thread_id", "")
                    )
        except Exception:
            pass
        return ""

    # ── 事件记录辅助 ────────────────────────────────────────────

    def _record(
        self,
        thread_id: str,
        event_type: EventType,
        data: Optional[dict] = None,
        task_id: str = "",
    ) -> None:
        """写入一条事件到 EventStore。"""
        if not thread_id:
            return
        try:
            self._store.insert_event(
                TraceEvent(
                    thread_id=thread_id,
                    agent_type=self._agent_type,
                    task_id=task_id,
                    parent_thread_id=self._get_parent_thread_id(),
                    event_type=event_type,
                    timestamp=time.time(),
                    data=data or {},
                )
            )
        except Exception as e:
            _logger.debug("[TraceRecorder] record failed: %s", e)

    # ── wrap_model_call ─────────────────────────────────────────

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步 LLM 调用：记录事件。"""
        thread_id = self._get_thread_id(request)

        self._record(thread_id, EventType.LLM_CALL_START, {
            "model": getattr(request, "model_name", "unknown"),
            "message_count": len(getattr(request, "messages", [])),
        })

        t0 = time.time()
        try:
            response = handler(request)
        except Exception:
            self._record(thread_id, EventType.ERROR, {
                "phase": "llm_call",
                "error": "LLM call failed",
            })
            raise

        elapsed_ms = int((time.time() - t0) * 1000)
        usage = _extract_usage(response)

        # 记录 LLM_CALL_END 事件（含 token 用量，供事后回溯）
        self._record(thread_id, EventType.LLM_CALL_END, {
            "elapsed_ms": elapsed_ms,
            "usage": usage or {},
        })

        # 不写 state.token_stats —— Token 计量由 TokenMeterMiddleware 负责，
        # 避免两处同时 update 同一字段导致 reducer 累加重复。
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        """异步 LLM 调用：记录事件。"""
        thread_id = self._get_thread_id(request)

        self._record(thread_id, EventType.LLM_CALL_START, {
            "model": getattr(request, "model_name", "unknown"),
            "message_count": len(getattr(request, "messages", [])),
        })

        t0 = time.time()
        try:
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
        except Exception:
            self._record(thread_id, EventType.ERROR, {
                "phase": "llm_call",
                "error": "LLM call failed",
            })
            raise

        elapsed_ms = int((time.time() - t0) * 1000)
        usage = _extract_usage(result)

        # 记录 LLM_CALL_END 事件（含 token 用量，供事后回溯）
        self._record(thread_id, EventType.LLM_CALL_END, {
            "elapsed_ms": elapsed_ms,
            "usage": usage or {},
        })

        # 不写 state.token_stats —— 见 wrap_model_call 注释。
        return result

    # ── wrap_tool_call ──────────────────────────────────────────

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """同步工具调用：记录事件 + 检测 start_async_task。"""
        # 现取（不再读模型调用留下的实例属性）——见 _get_thread_id 的说明。
        # ToolCallRequest 同样带 runtime.execution_info.thread_id，与模型事件同源。
        thread_id = self._get_thread_id(request)
        tool_name = _get_tool_name(request)

        is_subagent_spawn = (
            tool_name == "start_async_task"
            and self._agent_type == "chat_agent"
        )

        self._record(thread_id, EventType.TOOL_CALL_START, {
            "tool": tool_name,
            "args_summary": _summarize_args(request),
        })

        try:
            result = handler(request)
        except Exception:
            self._record(thread_id, EventType.ERROR, {
                "phase": "tool_call",
                "tool": tool_name,
                "error": "Tool call failed",
            })
            raise

        if is_subagent_spawn:
            sub_thread_id = _extract_sub_thread_id(result)
            task_id = _extract_task_id(request) or sub_thread_id
            if sub_thread_id:
                self._record(
                    thread_id, EventType.SUBAGENT_SPAWN, {
                        "sub_agent_type": "nl2sql",
                        "sub_thread_id": sub_thread_id,
                        "task_id": task_id,
                    },
                    task_id=task_id,
                )
                # 建立谱系记录
                self._store.upsert_lineage(
                    thread_id=sub_thread_id,
                    parent_thread_id=thread_id,
                    agent_type="nl2sql_agent",
                    status="active",
                )
                _logger.info(
                    "[TraceRecorder] subagent spawn: parent=%s sub=%s",
                    thread_id[:8] if thread_id else "?",
                    sub_thread_id[:8],
                )

        self._record(thread_id, EventType.TOOL_CALL_END, {
            "tool": tool_name,
            "result_summary": _summarize_result(result),
        })

        return result

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """异步工具调用：记录事件 + 检测 start_async_task。"""
        thread_id = self._get_thread_id(request)  # 现取，见 _get_thread_id
        tool_name = _get_tool_name(request)

        is_subagent_spawn = (
            tool_name == "start_async_task"
            and self._agent_type == "chat_agent"
        )

        self._record(thread_id, EventType.TOOL_CALL_START, {
            "tool": tool_name,
            "args_summary": _summarize_args(request),
        })

        try:
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
        except Exception:
            self._record(thread_id, EventType.ERROR, {
                "phase": "tool_call",
                "tool": tool_name,
                "error": "Tool call failed",
            })
            raise

        if is_subagent_spawn:
            sub_thread_id = _extract_sub_thread_id(result)
            task_id = _extract_task_id(request) or sub_thread_id
            if sub_thread_id:
                self._record(
                    thread_id, EventType.SUBAGENT_SPAWN, {
                        "sub_agent_type": "nl2sql",
                        "sub_thread_id": sub_thread_id,
                        "task_id": task_id,
                    },
                    task_id=task_id,
                )
                self._store.upsert_lineage(
                    thread_id=sub_thread_id,
                    parent_thread_id=thread_id,
                    agent_type="nl2sql_agent",
                    status="active",
                )

        self._record(thread_id, EventType.TOOL_CALL_END, {
            "tool": tool_name,
            "result_summary": _summarize_result(result),
        })

        return result


# ── 辅助函数 ────────────────────────────────────────────────────

def _extract_usage(response: Any) -> dict[str, Any] | None:
    """从 ModelResponse 的 AIMessage 中提取 usage_metadata。"""
    if not isinstance(response, ModelResponse):
        return None
    for msg in getattr(response, "result", []) or []:
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            return usage
    return None


def _tc_field(tc: Any, key: str, default: Any = None) -> Any:
    """从 tool_call 里取字段，dict 与对象两种形态都支持。

    ⚠️ `ToolCall` 在本版本是 TypedDict —— **运行时就是普通 dict**，`getattr(tc,"args")`
    恒为 None。此前 `_summarize_args` 因此把每个工具事件的 args_summary 都写成 `"{}"`
    （查 trace 时看不出工具入参，等于白记）。这里统一按形态取值。
    """
    if tc is None:
        return default
    if isinstance(tc, dict):
        value = tc.get(key, default)
    else:
        value = getattr(tc, key, default)
    return default if value is None else value


def _get_tool_name(request: Any) -> str:
    """从 ToolCallRequest 中提取工具名称。"""
    try:
        return str(_tc_field(getattr(request, "tool_call", None), "name", "") or "unknown")
    except Exception:
        pass
    return "unknown"


def _summarize_args(request: Any) -> str:
    """提取工具调用参数的摘要（最多 200 字符）。"""
    try:
        args = _tc_field(getattr(request, "tool_call", None), "args", None)
        if isinstance(args, dict):
            text = str(args)
            return text[:200] + ("..." if len(text) > 200 else "")
    except Exception:
        pass
    return ""


def _summarize_result(result: Any) -> str:
    """提取工具返回结果的摘要（最多 200 字符）。"""
    try:
        # ToolMessage 的 content
        if hasattr(result, "content"):
            text = str(result.content)
            return text[:200] + ("..." if len(text) > 200 else "")
        # Command 的 update
        if hasattr(result, "update"):
            text = str(result.update)
            return text[:200] + ("..." if len(text) > 200 else "")
    except Exception:
        pass
    return ""


def _extract_sub_thread_id(result: Any) -> str:
    """从 start_async_task 返回的 Command 中提取子 agent 的 thread_id。"""
    try:
        # Command.update.async_tasks[task_id].thread_id
        update = getattr(result, "update", None) or {}
        async_tasks = update.get("async_tasks", {})
        for task_info in async_tasks.values():
            if isinstance(task_info, dict):
                tid = task_info.get("thread_id", "")
                if tid:
                    return tid
    except Exception:
        pass
    return ""


def _extract_task_id(request: Any) -> str:
    """从 start_async_task 的参数中提取 task_id（= subagent_type）。"""
    try:
        tc = getattr(request, "tool_call", None)
        if tc is not None:
            args = getattr(tc, "args", None) or {}
            if isinstance(args, dict):
                # task_id 通常 == subagent_type（如 "nl2sql"）
                # 但实际创建时 LangGraph 会分配 UUID
                return args.get("subagent_type", "")
    except Exception:
        pass
    return ""