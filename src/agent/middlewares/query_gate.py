"""QueryGateMiddleware — nl2sql 子 agent 查询执行前的确定性兜底（双规则）。

背景（方案 A2 前段三合一，2026-09-04；通道硬闸 2026-09-05）：
nl2sql 子 agent 有两条查询通道：``wrenai_<库名>_*``（Wren 语义层）与 ``dbmcp_*``
（db_mcp_server 直连，绕过语义层）。生产 trace（bbb0eda0）实证：WIT 这类**已建模**库
上模型仍直连 ``dbmcp_run_sql`` ×21 次——``dynamic_prompt`` 的「查询通道路由」只是文本
指引，模型不遵守；QueryGate 原「先获取后执行」软闸被 ``dbmcp_get_db_info`` 满足后即全
放行。故本中间件升级为**通道 + 顺序**双规则：

规则一（通道硬闸，每次必拦，非 once）：
  当前查询的数据库**已在 Wren 语义层建模**时，``dbmcp_*``（run_sql 与 get_db_info
  都算）**一律不执行**，返回 ``status="error"`` 指到 ``wrenai_<库>_run_sql`` 等语义
  工具。目标库解析：优先工具参数 ``db_name``，缺省读 state 系统提示的「当前数据库:
  `X` —— 已在/未在 Wren 语义层建模」标记（与 dynamic_prompt 注入同一来源）。
  未建模库仍允许 dbmcp（那是它们唯一查询通道）。

规则二（顺序软提醒，沿用原逻辑）：
  run_sql/dry_run/dry_plan 执行前若本轮从未出现过任何「获取工具」，提醒先按
  nl2sql-understand 完成清晰度裁决 + 知识 + Schema 再查（每线程至多一次）。

软 / 防打扰设计：
- **每线程至多提醒一次**（``_REMINDED`` set）：规则二首次拦下给指导，此后该线程放行——
  不无限弹回、不 interrupt、不走 permission 卡。
- **Cube 通道完全豁免**（list_cubes/describe_cube/query_cube）：策略 C 无需 A/B 检索。
- 用户直接给出精确 SQL 时若被误拦，只浪费一轮（模型读指导后重发即放行），可接受。
- thread_id / 当前库标记取不到 → fail-open 放行（与 fs_thread_guard :138-141 一致）。

``status="error"`` + ``Error:`` 前缀让 langfuse_span._maybe_score 把它当 exec 失败
（``looks_like_exec_error``），避免被误记成 sql_exec_success=1（SqlReadOnly._deny 同款）。

挂载：nl2sql_agent._middleware，位于 sql_approval_middleware 之前（LangfuseSpan 内层、
SqlReadOnly 硬闸外层 → 被拦的 run_sql 仍在 span 里可见，写/DDL 仍由硬闸兜底）。
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

_logger = logging.getLogger(__name__)

# ── 工具名单（wrenai_<库名>_run_sql / dbmcp_run_sql 等前缀皆靠子串/后缀命中）──

# 后获取工具 = 真正查询/执行（需要先做过理解建模才有意义）
_LATE_EXACT = {"run_sql", "dry_run", "dry_plan"}

# 获取工具证据 = 出现过任一即视为「已做过获取」。describe_model 也算（它只在
# describe_schema/get_context 之后才被允许出现，有它即意味着已有 schema 依据）。
_FETCH_SUBS = (
    "get_context", "get_instructions", "recall_queries", "get_all_knowledge",
    "describe_schema", "describe_model", "get_mdl", "list_models",
    "get_db_info", "get_data_source", "list_functions", "list_stored_queries",
)

# Cube 通道工具——一律不过闸（策略 C 唯一合法通道）
_CUBE_EXACT = {"list_cubes", "describe_cube", "query_cube"}

# 线程级「已提醒过」去重（进程内；线程随 run 复用，FIFO 上限防泄漏）
_REMINDED: set[str] = set()
_REMINDED_LOCK = threading.Lock()
_REMINDED_CAP = 4000

_HINT = (
    "Error: 检测到尚未完成「理解建模」就直接发起 {tool}。请先按 nl2sql-understand "
    "skill 的顺序：先 get_context + get_instructions 做清晰度裁决与知识加载"
    "（问题不清晰时应先以 [需要澄清] 向用户追问，不要继续），确认 clear 后再获取 "
    "Schema（describe_schema / get_mdl / describe_model），最后重新发起 {tool}。"
)

# 通道硬闸：已建模库禁止走 dbmcp 直连（不经过语义层，缺业务口径）。每次必拦。
_CHANNEL_HINT = (
    "Error: 当前数据库「{db}」已在 Wren 语义层建模，禁止直连查询（dbmcp_* 不经过"
    "语义层，会丢业务口径）。请改用语义层工具：执行用 `{prefix}_run_sql(sql, limit?)`；"
    "schema/知识用 `{prefix}_get_data_source` / `{prefix}_describe_model` / "
    "`{prefix}_list_knowledge` / `{prefix}_list_stored_queries` 等。不要用 "
    "dbmcp_run_sql / dbmcp_get_db_info 查该库。"
)

# 直连通道工具（db_server.py 暴露 run_sql/get_db_info，工具名带 dbmcp_ 前缀）
_DBMCP_PREFIX = "dbmcp_"

# state 系统提示里 dynamic_prompt 注入的路由标记（与 nl2sql_agent.dynamic_prompt 同源）
_ACTIVE_DB_RE = re.compile(
    r"当前数据库:\s*`([^`]+)`\s*——\s*(已在\s*Wren\s*语义层建模|未在\s*语义层建模)"
)


def _tool_name(request: ToolCallRequest) -> str:
    tc = getattr(request, "tool_call", None) or {}
    if isinstance(tc, dict):
        return str(tc.get("name", "") or "")
    return str(getattr(tc, "name", "") or "")


def _tool_args(request: ToolCallRequest) -> dict:
    try:
        tc = getattr(request, "tool_call", None)
        if tc is not None:
            if isinstance(tc, dict):
                args = tc.get("args")
                return args if isinstance(args, dict) else {}
            args = getattr(tc, "args", None)
            return args if isinstance(args, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _is_dbmcp(name: str) -> bool:
    """直连通道工具：dbmcp_run_sql / dbmcp_get_db_info / dbmcp_*。"""
    return name == "dbmcp" or name.startswith(_DBMCP_PREFIX)


def _active_db(request: ToolCallRequest) -> tuple[str, bool]:
    """解析当前查询库 + 是否已建模。优先工具参数 db_name，缺省读 state 系统提示的
    「查询通道路由」标记（与 dynamic_prompt 注入同源）。返回 (db_name, modeled)；
    取不到 → ("", False)（调用方按未建模/未知放行，勿误伤未建模库的合法直连）。
    """
    try:
        from agent.utils.semantic_db import get_detector  # noqa: E402
        det = get_detector()
        # 1) 工具显式 db_name（dynamic_prompt 要求 dbmcp 直连必须传 db_name）
        args = _tool_args(request)
        dn = str(args.get("db_name") or "").strip()
        if dn:
            try:
                return dn, bool(det.is_modeled(dn))
            except Exception:  # noqa: BLE001
                return dn, False
        # 2) state 系统提示路由标记兜底
        for m in _state_messages(getattr(request, "state", None)):
            content = ""
            try:
                content = str(m.content or "")
            except Exception:  # noqa: BLE001
                continue
            if "当前数据库" in content and "语义层建模" in content:
                hit = _ACTIVE_DB_RE.findall(content)
                if hit:
                    db, flag = hit[-1]
                    return db, ("已" in flag)
    except Exception:  # noqa: BLE001
        pass
    return "", False


def _is_late(name: str) -> bool:
    return name in _LATE_EXACT or name.endswith("_run_sql")


def _is_fetch_tool(name: str) -> bool:
    return any(sub in name for sub in _FETCH_SUBS)


def _state_messages(state: Any) -> list:
    """从 ToolCallRequest.state（dict/list/BaseModel）取消息列表。"""
    if state is None:
        return []
    if isinstance(state, dict):
        return list(state.get("messages") or [])
    msgs = getattr(state, "messages", None)
    if msgs is not None:
        return list(msgs)
    if isinstance(state, (list, tuple)):
        return list(state)
    return []


def _ever_fetched(state: Any) -> bool:
    """反扫历史 assistant 消息的 tool_calls，任一获取工具名 → True。

    含与当前 run_sql **同一批次**出现的 FETCH（最后一条 AIMessage 的 tool_calls
    里可能既有 get_context 又有 run_sql），天然放行同批并行。
    """
    for m in _state_messages(state):
        try:
            tcs = list(getattr(m, "tool_calls", None) or [])
        except Exception:  # noqa: BLE001
            continue
        for tc in tcs:
            n = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if _is_fetch_tool(str(n or "")):
                return True
    return False


def _thread_id(request: ToolCallRequest) -> str:
    """去重作用域：优先子 agent 自己的 thread（request.runtime），跨 run 自动隔离。

    取不到 → 返回 ""（调用方 fail-open 放行）。
    """
    try:
        rt = getattr(request, "runtime", None)
        if rt is not None:
            ei = getattr(rt, "execution_info", None)
            if ei is not None:
                tid = getattr(ei, "thread_id", "") or getattr(ei, "thread", "")
                if tid:
                    return str(tid)
    except Exception:  # noqa: BLE001
        pass
    try:
        from langgraph.config import get_config as _cfg
        cfg = _cfg()
        if cfg:
            configurable = cfg.get("configurable") or {}
            for key in ("trace_parent_thread_id", "thread_id"):
                v = configurable.get(key)
                if v:
                    return str(v)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _mark_reminded(tid: str) -> bool:
    """返回 True=本轮应拦；False=已提醒过/无需再拦。"""
    with _REMINDED_LOCK:
        if tid in _REMINDED:
            return False
        if len(_REMINDED) > _REMINDED_CAP:
            _REMINDED.clear()
        _REMINDED.add(tid)
        return True


class QueryGateMiddleware(AgentMiddleware):
    """在 run_sql/dry_run/dry_plan 执行前兜底提醒先完成理解建模。"""

    def _channel_deny(
        self, request: ToolCallRequest, name: str, db: str
    ) -> ToolMessage:
        """通道硬闸拒绝：返回指到 wrenai 语义工具的错误 ToolMessage。"""
        prefix = ""
        try:
            from agent.utils.semantic_db import wrenai_server_name  # noqa: E402
            prefix = wrenai_server_name(db)
        except Exception:  # noqa: BLE001
            pass
        tool_call_id = (
            getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""
        )
        _logger.info(
            "[query_gate] 通道硬闸：已建模库 %s 上拦截直连工具 %s",
            db, name,
        )
        return ToolMessage(
            content=_CHANNEL_HINT.format(db=db, prefix=prefix),
            name=name,
            tool_call_id=tool_call_id,
            status="error",
        )

    def _gate(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        name = _tool_name(request)
        # 通道硬闸（规则一）：当前库已建模 → dbmcp_*（run_sql/get_db_info 皆拦，每次）
        if _is_dbmcp(name):
            db, modeled = _active_db(request)
            if db and modeled:
                return self._channel_deny(request, name, db)
        # 非后获取工具 / Cube 通道 / 已做过获取 → 直接放行
        if name in _CUBE_EXACT or not _is_late(name):
            return handler(request)
        if _ever_fetched(getattr(request, "state", None)):
            return handler(request)
        tid = _thread_id(request)
        if not tid:
            return handler(request)  # 取不到线程标识 → fail-open
        if not _mark_reminded(tid):
            return handler(request)  # 已提醒过一次 → 放行（软门，不无限弹回）

        tool_call_id = (
            getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""
        )
        _logger.info(
            "[query_gate] 拦截未先做理解建模的 %s（线程 %s，软提醒一次）",
            name, tid[:8],
        )
        return ToolMessage(
            content=_HINT.format(tool=name),
            name=name,
            tool_call_id=tool_call_id,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        return self._gate(request, handler)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        result = self._gate(request, handler)
        if hasattr(result, "__await__"):
            return await result
        return result
