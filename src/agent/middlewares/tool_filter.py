"""ToolFilterMiddleware — 按当前数据库名过滤工具，只暴露当前库的 wrenai 工具。

路线 A（多 server 常驻 + 按库过滤）：
- 所有 MCP server 启动时全部加载（现状不变）
- 每次 LLM 调用时，按 configurable.db_name 筛出当前库的工具
- 其他库的 wrenai 工具对 LLM 不可见，提示词更干净，选工具更准

过滤规则：
- wrenai_<当前库名>_* → 保留（当前库的语义层工具）
- wrenai_<其他库名>_* → 过滤（其他库的语义层工具，LLM 不需要）
- dbmcp_* → 当前库**有**语义层工具时移除（语义层独占，见下），否则保留
- 非 wrenai/dbmcp 的工具 → 保留（如图表工具等）

语义层独占（2026-09-09）：
已建模库上 dbmcp 直连不经过语义层、丢业务口径，此前靠 QueryGate 事后拦截
（模型先试一次 → 收 status=error → 重读指引 → 重发语义层工具，白费一轮）。
改为在出站 payload 里**直接不绑定** dbmcp_*：模型看不到就不会试，且与
dynamic_prompt 的「已建模只讲 wrenai / 未建模只讲 dbmcp」二分支对齐
（此前 prompt 说禁止而工具还绑着，模型才会去试）。

判定用**实际存在的工具**（name.startswith(prefix)）而非 is_modeled()：后者查的是
db_config 配置，wrenai server 加载失败时（2026-09-08 预检事故）配置仍显示已建模，
据此移除 dbmcp 会让模型一个查询工具都没有 —— 必须 fail-open 保留。
QueryGate 通道硬闸保留为第二道防线：历史消息里的存量 dbmcp 调用、模型从静态
prompt 幻觉出的工具名，仍由它接住（看不见 + 拦得住是纵深，非二选一）。

原理：ModelRequest.override(tools=[...]) 创建新的 ModelRequest 实例，
替换 tools 列表，LangGraph 在后续模型调用中只传递新列表。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

# 语义层独占开关：默认开（当前库有 wrenai 工具时不绑 dbmcp_*）。
# 置 0/false/no/off 回退旧行为（dbmcp 常驻，仅靠 QueryGate 事后拦截）——出问题
# 可免改代码秒回退。
_EXCLUSIVE_ENV = "NL2SQL_SEMANTIC_EXCLUSIVE_TOOLS"


def semantic_exclusive_enabled() -> bool:
    """语义层独占是否开启（默认开）。"""
    return (os.getenv(_EXCLUSIVE_ENV, "") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _is_dbmcp(name: str) -> bool:
    """直连通道工具（与 query_gate._is_dbmcp 同口径）：dbmcp_run_sql / dbmcp_get_db_info。"""
    return name == "dbmcp" or name.startswith("dbmcp_")


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
        r"""由 db_name 推导 wrenai 工具前缀，如 'imdb' → 'wrenai_imdb_'。

        统一走 semantic_db.wrenai_server_name（唯一的净化源），避免二次实现
        净化逻辑漂移：库名含中文时旧 `\W+` 规则不折叠 CJK，过滤前缀会带着
        中文匹配不上 ASCII 工具名（见 semantic_db._server_slug 注释）。
        """
        from agent.utils.semantic_db import wrenai_server_name

        return wrenai_server_name(db_name) + "_"

    def _filter_tools(self, tools: list, db_name: str) -> list:
        """按 db_name 过滤工具列表。

        保留规则：
        1. wrenai_<当前库名>_* → 保留；wrenai_<其他库名>_* → 过滤
        2. dbmcp_* → 当前库**有**语义层工具（实际存在 wrenai_<前缀>_*）且开关开启
           时移除；否则保留（fail-open：未建模库 / wrenai server 加载失败时 dbmcp
           是唯一查询通道）
        3. 非 wrenai/dbmcp 工具 → 保留（图表工具等）
        """
        if not db_name:
            return tools

        prefix = self._get_wrenai_prefix(db_name)
        exclusive = semantic_exclusive_enabled()

        kept: list = []
        skipped = 0
        dbmcp: list = []
        has_current_wrenai = False
        for t in tools:
            name = getattr(t, "name", "") or ""
            if name.startswith("wrenai_"):
                if name.startswith(prefix):
                    has_current_wrenai = True
                    kept.append(t)
                else:
                    skipped += 1
            elif _is_dbmcp(name):
                dbmcp.append(t)  # 去留取决于当前库语义层是否可用（见下）
            else:
                kept.append(t)

        dropped_dbmcp = 0
        if has_current_wrenai and exclusive:
            dropped_dbmcp = len(dbmcp)  # 语义层独占：不绑直连
        else:
            kept.extend(dbmcp)

        if skipped or dropped_dbmcp:
            _logger.info(
                "[ToolFilter] db_name=%s prefix=%s → 保留 %d 工具，过滤其他库 wrenai %d 个%s",
                db_name, prefix, len(kept), skipped,
                (f"，语义层独占移除 dbmcp {dropped_dbmcp} 个" if dropped_dbmcp else ""),
            )
        return kept

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