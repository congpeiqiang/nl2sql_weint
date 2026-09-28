"""DynamicMCPToolsMiddleware — 运行期新增/删除库的 MCP 工具即时生效（免重启）。

背景
----
此前管理员新增/删除一个库或语义库后**必须重启后端**：`ToolNode.tools_by_name`
在 graph 编译时固化（`nl2sql_agent` 在 import 期就把 `sub_tools` 列表交给
`create_deep_agent`），进程起来后新库的工具进不了那条通道。而发现层本来就是热的
（`SemanticDbDetector.invalidate()` 已由 db_config / 语义库 API 调用），语义库
**内容**更新也从来不需要重启（每次工具调用新起 MCP 子进程读 `target/mdl.json`）。

官方给的两条动态通道（langchain `agents/factory.py` 的
`DYNAMIC_TOOL_ERROR_TEMPLATE`）正好覆盖两半：
- 模型侧：`wrap_model_call` 里 `request.override(tools=...)` —— 本轮能看见哪些工具；
- 执行侧：`wrap_tool_call` 里 `request.override(tool=...)` —— 这个名字执行哪个实例。
（`ToolNode._run_one` 对未注册工具**延迟校验**、`_execute_tool_sync` 用
`request.tool` 执行，所以 override 进来的实例会被真正执行；本仓已有 8 个
`wrap_tool_call` 中间件 → factory 的未知工具校验早已关闭。）

本中间件是这两条通道的唯一使用者，数据源是 `mcp_tool` 的运行期注册表
（`sub_tools_snapshot` / `lookup_sub_tool` / `sub_registry_warmed`）：

1. **并入**：注册表里有、请求清单里还没有的工具（管理员刚加的库）→ 追加，当轮可见；
2. **摘除**：看着是子工具（`wrenai_*` / `dbmcp*`）但注册表里已经没有的名字
   （库被删/改名/重新关联，或本部署的 wren 不提供该工具）→ 从清单摘掉，模型看不到就不会去试；
3. **改道**：执行时按名字取注册表里的**当前**实例（重加载后静态列表里那个是陈旧的）
   → `override(tool=...)`；
4. **拒绝**：注册表已预热却仍收到已下线工具的调用（历史消息里的存量 tool_call、
   提示词里写了但本部署工具面并没有的名字）→ 返回 `status="error"` 的 ToolMessage，
   **绝不执行陈旧实例**，并带上不带因果断言的替代指引（见 `_REMOVED_HINT`）。

fail-open 边界：注册表**未预热**（离线单测、启动加载尚未跑完）时只并入、不摘除、
不拒绝 —— 绝不因"注册表里没有"把健康工具误杀。预热 = 完成过一次全量对账。

顺序：必须挂在 `_middleware` **最前**（`ToolFilterMiddleware` 的外层）—— 先并入
新库工具，再让 ToolFilter 按当前库裁剪；挂反了新库工具会绕过按库过滤。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")


def _is_sub_tool(name: str) -> bool:
    """是否子智能体的查询通道工具（与 tool_filter._is_dbmcp / query_gate 同口径）。"""
    return name.startswith("wrenai_") or name == "dbmcp" or name.startswith("dbmcp_")


def _tool_name(request: ToolCallRequest) -> str:
    call = getattr(request, "tool_call", None) or {}
    if isinstance(call, dict):
        return call.get("name") or ""
    return getattr(call, "name", "") or ""


def _tool_call_id(request: ToolCallRequest) -> str:
    """取 tool_call_id：优先 tool_call 自带的 id（与 AIMessage 严格配对）。"""
    call = getattr(request, "tool_call", None) or {}
    cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
    if cid:
        return cid
    return getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""


# 拒绝文案：**只许陈述可证事实**。`_resolve` 能证明的只有「这个名字不在已预热的
# 注册表里」，至少有三种成因：① 库/语义库被删或重新关联；② 本部署的 wren 版本压根
# 不提供这个工具；③ 名字拼写/前缀漂移（白名单与真实工具名不一致）。
# ⚠️ 不得断言其中任何一种 —— 这条 ToolMessage 会被模型当事实**转述给用户**。
# 血案（2026-09-26 `get_all_knowledge`）：文案断言「库已被删除或重新关联」，真因是
# **上游 wren 0.13.0~0.15.0 全都没有这个工具**（是提示词在推荐一个不存在的工具），
# 用户因此以为语义库被人删了。
_REMOVED_HINT = (
    "工具 {tool} 当前不在可用工具清单里（可能的原因：它所属的库已被删除或重新关联，"
    "或本次部署的语义服务/数据源不提供这个工具）。"
    "本轮不要再用它、也不要重试。请改用当前可用的工具："
    "已建模库走 wrenai_<库名>_* 语义工具（如 get_context / run_sql），"
    "未建模库走 dbmcp_run_sql。{fallback}"
)

# 已知「能力性工具」的替代做法（名字后缀 → 建议）。这些名字在上游不同版本的工具面里
# 有出入，这里给的替代路径是本部署**确定存在**的等价能力。
_TOOL_FALLBACKS = {
    "get_all_knowledge": "本轮的替代做法：用 list_knowledge() 列出知识文件（metrics/glossary/caveats），再按需读取相关文件。",
    "describe_schema": "本轮的替代做法：用 get_context(question) 取 Schema 片段（已含列/类型/FK/measure 定义）。",
    "get_mdl": "本轮的替代做法：用 get_context(question) 取相关 model 片段，或用 list_models() 看模型清单。",
    "describe_model": "本轮的替代做法：用 get_context(question) 或 list_models()。",
}


def _fallback_for(name: str) -> str:
    """按名字后缀匹配替代做法（兼容 `wrenai_<库>_get_all_knowledge` 这类带前缀的名字）。

    无匹配时返回空串 —— 文案仍然完整，只是不给具体替代（绝不因查表失败而丢消息）。
    """
    for suffix, hint in _TOOL_FALLBACKS.items():
        if name == suffix or name.endswith("_" + suffix):
            return " " + hint
    return ""


class DynamicMCPToolsMiddleware(AgentMiddleware):
    """把运行期工具注册表接进模型清单与执行链（见模块 docstring）。"""

    # ── 模型侧：并入新增工具 / 摘除已下线工具 ──────────────────
    def _sync_tools(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        from agent.tools.mcp_tool import sub_registry_warmed, sub_tools_snapshot

        current = list(getattr(request, "tools", None) or [])
        fresh = sub_tools_snapshot()
        warmed = sub_registry_warmed()

        dropped = 0
        tools = current
        if warmed:
            available = {getattr(t, "name", "") or "" for t in fresh}
            tools = [
                t for t in current
                if not (
                    _is_sub_tool(getattr(t, "name", "") or "")
                    and (getattr(t, "name", "") or "") not in available
                )
            ]
            dropped = len(current) - len(tools)

        known = {getattr(t, "name", "") or "" for t in tools}
        added = [t for t in fresh if (getattr(t, "name", "") or "") not in known]
        if not added and not dropped:
            return request

        _logger.info(
            "[DynamicMCPTools] 工具清单 %d → %d（并入 %d，摘除 %d%s）",
            len(current), len(tools) + len(added), len(added), dropped,
            "" if warmed else "，注册表未预热",
        )
        return request.override(tools=tools + added)

    # ── 执行侧：按名字取当前实例 / 拒绝已下线工具 ──────────────
    def _resolve(
        self, request: ToolCallRequest
    ) -> Any:
        """返回 override 后的 request，或直接返回错误 ToolMessage（短路执行）。"""
        from agent.tools.mcp_tool import lookup_sub_tool, sub_registry_warmed

        name = _tool_name(request)
        if not name or not _is_sub_tool(name):
            return request

        registered = lookup_sub_tool(name)
        if registered is not None:
            if registered is not getattr(request, "tool", None):
                # 库被重新关联/重加载过 → 用注册表里的当前实例，不用静态列表里的陈旧实例
                return request.override(tool=registered)
            return request

        if not sub_registry_warmed():
            # 未预热：只有静态列表里的实例可依据 → 照常执行（fail-open）
            return request

        _logger.warning(
            "[DynamicMCPTools] 拒绝不在可用清单里的工具 %s（注册表已预热且无此名字）", name
        )
        return ToolMessage(
            content=_REMOVED_HINT.format(tool=name, fallback=_fallback_for(name)),
            name=name,
            tool_call_id=_tool_call_id(request),
            status="error",
        )

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        return handler(self._sync_tools(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        result = handler(self._sync_tools(request))
        if hasattr(result, "__await__"):
            return await result
        return result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        resolved = self._resolve(request)
        if isinstance(resolved, ToolMessage):
            return resolved
        return handler(resolved)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        resolved = self._resolve(request)
        if isinstance(resolved, ToolMessage):
            return resolved
        result = handler(resolved)
        if hasattr(result, "__await__"):
            return await result
        return result
