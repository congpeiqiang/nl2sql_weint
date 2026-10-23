"""固化回放模式：本轮数据已由系统取回，主 agent 只负责解读与出报告。

回放 run 的 ``input`` 里已经塞了一条成功的 ``check_async_task`` 结果（见
``scheduler/worker._build_replay_input``）。可模型并不知道这件事——不拦的话它会
照常 ``start_async_task`` 再派一遍子 agent，于是同一份数据取两次、两套口径。

两道闸，缺一不可：

- **工具面**（本文件）：``configurable.pinned_replay`` 为真时把
  ``start_async_task`` 从模型可见工具里摘掉——看不见就不会试（与
  ``tool_filter`` 的「语义层独占」同一思路：提前不绑，胜过事后拦）。
- **提示词**（``main_agent.dynamic_prompt`` 拼 ``PINNED_REPLAY_SECTION``）：
  告诉模型数据在哪、哪几条不许做。

提示词段用**代码注入**而不是写进 ``MAIN_AGENT_PROMPT.md``：那份文件的线上生效
副本由 Langfuse 下发，改仓库里的 md 不会上线。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

#: 回放本轮必须消失的委派工具。只摘这一个：图表、报告、定时任务管理等都要留着。
DELEGATION_TOOL = "start_async_task"

PINNED_REPLAY_SECTION = """## 固化回放模式（本次运行）— 最高优先级
本轮数据已由系统按「已固化查询」取回，结果就在最近一条 check_async_task 的 ToolMessage 里
（其中 pinned_replay=true）。本次运行的规则：
1. 禁止委派 nl2sql、禁止调用任何查询工具重新取数——本任务的工具面已移除委派能力。
2. 禁止等待或轮询子任务；数据已完整。
3. 直接基于该结果生成图表与报告；报告的「执行 SQL」节使用结果里的 sql / cube_query 原文。
4. 结果为空或截断时如实说明「未查询到数据」/「结果已截断」，不得补充推测。

"""


def _cfg() -> dict:
    """读当前 run 的 configurable。

    ``request.runtime.config`` 恒为空（2026-08-11 实证），必须走
    ``langgraph.config.get_config()``——与 ``tool_filter._resolve_db_name`` 同路径。
    单独成函数是为了测试能 monkeypatch。
    """
    try:
        from langgraph.config import get_config as _get

        if _get is not None:
            return (_get().get("configurable", {}) or {})
    except Exception:  # noqa: BLE001
        pass
    return {}


class PinnedReplayMiddleware(AgentMiddleware):
    """``configurable.pinned_replay`` 为真时摘掉委派工具。"""

    def _strip(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        if not _cfg().get("pinned_replay"):
            return request
        tools = list(getattr(request, "tools", None) or [])
        kept = [t for t in tools if getattr(t, "name", "") != DELEGATION_TOOL]
        if len(kept) == len(tools):
            return request  # 本来就没有，不必新建对象
        _logger.info("[PinnedReplay] 回放模式：摘掉 %s（工具 %d → %d）",
                     DELEGATION_TOOL, len(tools), len(kept))
        return request.override(tools=kept)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        return handler(self._strip(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        # 与 tool_filter 同款：handler 可能同步也可能异步（langchain 各版本不一）。
        result = handler(self._strip(request))
        if hasattr(result, "__await__"):
            return await result
        return result
