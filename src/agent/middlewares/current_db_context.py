"""CurrentDbContextMiddleware — 把当前数据库名注入最新一条用户消息。

解决「切库后主 agent 仍按旧库委派」的上下文污染问题：
- 实证（2026-08-11）：前端切库 clickhouse 后发"有多少表"，system prompt 已注入
  "当前数据库是 clickhouse"，但模型采信对话历史/总结里自己上轮说过的
  "当前应用数据库是 imdb"，仍按 imdb 委派（start_async_task 的【数据库名称】写错）。
- 本中间件在每次模型调用前，把 `configurable.db_name` 以 `【当前数据库：{db_name}】`
  前缀直接拼进最新一条用户消息，使其成为**当轮用户侧最高优先级的信号**，
  无法被历史/总结中的陈旧库名覆盖（system prompt 前置权威化见 main_agent.dynamic_prompt）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

# 消息前缀：当轮用户侧最高优先级信号
_PREFIX_TEMPLATE = "【当前数据库：{db_name}】"


class CurrentDbContextMiddleware(AgentMiddleware):
    """把当前库名（configurable.db_name）注入最新用户消息，当轮最高优先级。"""

    def _resolve_db_name(self, request: ModelRequest[ContextT]) -> str:
        # 注：langchain 官方明确 Runtime 不含 config —— ModelRequest.runtime.config
        # 恒为空 dict，读不到 configurable（实证 2026-08-11，db_name=''）。必须走
        # langgraph.config.get_config()（与 main_agent.dynamic_prompt 同一路径，实证有效）。
        try:
            from langgraph.config import get_config as _cfg
            if _cfg is not None:
                db_name = (_cfg().get("configurable", {}) or {}).get("db_name", "") or ""
                if db_name:
                    return db_name
        except Exception:  # noqa: BLE001
            pass
        # 兜底（当前 langchain 版本恒为空，保留以便旧框架兼容）
        try:
            runtime = getattr(request, "runtime", None)
            config = getattr(runtime, "config", None) or {}
            return (config.get("configurable") or {}).get("db_name", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _inject(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        db_name = self._resolve_db_name(request)
        if not db_name:
            return request
        messages = getattr(request, "messages", None) or []
        if not messages:
            return request
        last = messages[-1]
        if not isinstance(last, HumanMessage):
            # 仅当最新消息是用户消息时注入；工具回环/续跑/auto-continue 阶段不注入
            return request
        prefix = _PREFIX_TEMPLATE.format(db_name=db_name)
        content = last.content
        if isinstance(content, str):
            if content.startswith(prefix):
                return request  # 幂等
            new_content = prefix + content
        else:
            # content 为 block 列表：在末尾追加文本块（兼容 content_and_artifact 形态）
            try:
                new_content = list(content) + [{"type": "text", "text": f"\n{prefix}"}]
            except Exception:  # noqa: BLE001
                return request
        new_msg = HumanMessage(
            content=new_content,
            additional_kwargs=dict(last.additional_kwargs or {}),
            id=last.id,
        )
        _logger.info("[dbctx] 注入当前库名到最新用户消息: db_name=%s prefix=%r", db_name, prefix)
        return request.override(messages=messages[:-1] + [new_msg])

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步模型调用：注入后调用 handler。"""
        return handler(self._inject(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        """异步模型调用：注入后调用 handler。"""
        modified = self._inject(request)
        result = handler(modified)
        if hasattr(result, "__await__"):
            return await result
        return result
