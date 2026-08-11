"""QueryKeywordsMiddleware — 从运行时 context 读取查询关键词并注入系统提示词。

解决「前后端关键词一致性」问题：
- 前端把查询关键词（localStorage 配置）随 stream.submit 传入 configurable.query_keywords
- 本中间件在每次模型调用前，从 request.runtime.config.configurable 读取关键词，
  动态替换系统提示词中「数据查询触发关键词」段落，让 LLM 用与前端完全一致的
  关键词判断是否委派 nl2sql 子任务。
- 改词只需改前端 localStorage，前后端同步生效，零漂移。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

# MAIN_AGENT_PROMPT.md 中「数据查询触发关键词」行的标记，用于定位并替换
_KEYWORDS_MARKER = "**触发关键词**:"
# 默认关键词（前端未传时兜底，与 MAIN_AGENT_PROMPT.md 保持一致）
_DEFAULT_KEYWORDS = "查询、统计、分析、多少、列表、汇总、排名、占比、趋势"


class QueryKeywordsMiddleware(AgentMiddleware):
    """从 context.configurable.query_keywords 读取关键词，注入系统提示词。"""

    def __init__(
        self,
        default_keywords: str = _DEFAULT_KEYWORDS,
        marker: str = _KEYWORDS_MARKER,
    ) -> None:
        self.default_keywords = default_keywords
        self.marker = marker

    # ── 工具方法 ──────────────────────────────────────────────

    def _resolve_keywords(self, request: ModelRequest[ContextT]) -> str:
        """从运行时 config 读取前端传入的查询关键词，缺失时回退默认。"""
        try:
            runtime = getattr(request, "runtime", None)
            config = getattr(runtime, "config", None) or {}
            configurable = config.get("configurable") or {}
            kw = configurable.get("query_keywords")
            if isinstance(kw, (list, tuple)) and kw:
                return "、".join(str(k) for k in kw if str(k).strip())
            if isinstance(kw, str) and kw.strip():
                return kw
        except Exception as e:
            _logger.debug("[QueryKeywords] 读取 configurable 失败: %s", e)
        return self.default_keywords

    def _inject_keywords(
        self, request: ModelRequest[ContextT]
    ) -> ModelRequest[ContextT]:
        """替换系统提示词中的「触发关键词」行。"""
        keywords = self._resolve_keywords(request)
        system_message = request.system_message
        if system_message is None:
            return request

        text = system_message.text or ""
        if self.marker not in text:
            # 提示词中无标记行，直接追加一段（兜底）
            new_text = f"{text}\n\n{self.marker} {keywords}"
        else:
            # 替换标记行：只保留替换后的关键词行，避免新旧两行同时出现
            lines = text.split("\n")
            new_lines = []
            for line in lines:
                if self.marker in line:
                    new_lines.append(f"{self.marker} {keywords}")
                else:
                    new_lines.append(line)
            new_text = "\n".join(new_lines)

        from langchain_core.messages import SystemMessage

        new_system_message = SystemMessage(content=new_text)
        _logger.info("[QueryKeywords] 注入查询关键词: %s", keywords)
        return request.override(system_message=new_system_message)

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步模型调用：注入关键词后调用 handler。"""
        modified = self._inject_keywords(request)
        return handler(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Any
        ],
    ) -> ModelResponse[ResponseT]:
        """异步模型调用：注入关键词后调用 handler。"""
        modified = self._inject_keywords(request)
        result = handler(modified)
        if hasattr(result, "__await__"):
            return await result
        return result
