"""ToolFilterMiddleware — 按当前数据库名过滤工具，只暴露当前库的 wrenai 工具。

路线 A（多 server 常驻 + 按库过滤）：
- 所有 MCP server 启动时全部加载（现状不变）
- 每次 LLM 调用时，按 configurable.db_name 筛出当前库的工具
- 其他库的 wrenai 工具对 LLM 不可见，提示词更干净，选工具更准

过滤规则：
- wrenai_<当前库名>_* → 保留（当前库的语义层工具）
- wrenai_<其他库名>_* → 过滤（其他库的语义层工具，LLM 不需要）
- dbmcp_* → 保留（通用直连工具，按 db_name 参数路由）
- 非 wrenai/dbmcp 的工具 → 保留（如图表工具等）

原理：ModelRequest.override(tools=[...]) 创建新的 ModelRequest 实例，
替换 tools 列表，LangGraph 在后续模型调用中只传递新列表。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")


class ToolFilterMiddleware(AgentMiddleware):
    """按当前数据库名（configurable.db_name）过滤工具，只暴露当前库的 wrenai 工具。"""

    def _resolve_db_name(self) -> str:
        """从 LangGraph 运行时 config 读取当前数据库名。

        与 CurrentDbContextMiddleware / dynamic_prompt 同一路径：
        request.runtime.config 恒为空（实证 2026-08-11），必须走
        langgraph.config.get_config()。
        """
        try:
            from langgraph.config import get_config as _cfg

            if _cfg is not None:
                return (_cfg().get("configurable", {}) or {}).get("db_name", "") or ""
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _get_wrenai_prefix(self, db_name: str) -> str:
        """由 db_name 推导 wrenai 工具前缀，如 'imdb' → 'wrenai_imdb_'。"""
        import re
        return "wrenai_" + re.sub(r"\W+", "_", db_name or "").strip("_") + "_"

    def _filter_tools(self, tools: list, db_name: str) -> list:
        """按 db_name 过滤工具列表。

        保留规则：
        1. 非 wrenai_ 前缀的工具 → 保留（dbmcp_*、图表工具等）
        2. wrenai_<当前库名>_* → 保留
        3. wrenai_<其他库名>_* → 过滤
        """
        if not db_name:
            return tools

        prefix = self._get_wrenai_prefix(db_name)
        filtered = []
        skipped = 0
        for t in tools:
            name = getattr(t, "name", "") or ""
            if name.startswith("wrenai_"):
                if name.startswith(prefix):
                    filtered.append(t)
                else:
                    skipped += 1
            else:
                # 非 wrenai 工具（dbmcp_*、图表工具等）全部保留
                filtered.append(t)

        if skipped > 0:
            _logger.info(
                "[ToolFilter] db_name=%s prefix=%s → 保留 %d 工具，过滤 %d 个其他库工具",
                db_name, prefix, len(filtered), skipped,
            )
        return filtered

    def _filter(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """读取 db_name，过滤工具，返回新的 ModelRequest。"""
        db_name = self._resolve_db_name()
        if not db_name:
            return request

        tools = getattr(request, "tools", None) or []
        filtered = self._filter_tools(tools, db_name)
        if len(filtered) == len(tools):
            return request  # 无变化，不创建新对象

        # 统计日志：方便调试时确认过滤效果
        _logger.info(
            "[ToolFilter] db_name=%s 工具数: %d → %d (过滤 %d)",
            db_name, len(tools), len(filtered), len(tools) - len(filtered),
        )
        return request.override(tools=filtered)

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步模型调用：过滤工具后调用 handler。"""
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        """异步模型调用：过滤工具后调用 handler。"""
        modified = self._filter(request)
        result = handler(modified)
        if hasattr(result, "__await__"):
            return await result
        return result