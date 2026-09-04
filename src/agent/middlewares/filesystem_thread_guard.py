"""FilesystemThreadGuardMiddleware — nl2sql 子 agent 文件读权限的线程级护栏。

背景（同题多跑答案不一致归因 S2-1）：
nl2sql 子 agent 文件读权限此前整条 `allow /workspace/**`，可 read_file/grep/ls/glob
到其它会话/其它 run 的 `conversation_history/`、`report/`、以及
`nl2sql_process_data/{其它线程}/`（含 eval-subject 参考答案）→ 跨线程上下文泄漏，
答案变成「之前哪次会话怎么答的」的函数。

治理分两层：
- **静态层**（`file_permissions.py` 的 `NL2SQL_FILE_PERMISSIONS`）：读范围由
  `/workspace/**` 收窄为 `/shared/**` + `/workspace/large_tool_results/**` +
  `/workspace/nl2sql_process_data/**`，`conversation_history/`、`report/`、`tmp/`、
  工作区根文件等一律 `deny /**`（deepagents 对 read_file/ls/glob/grep 均强制
  `_check_fs_permission`，bulk 工具还带结果过滤）。
- **本中间件（运行时层）**：静态规则无法按线程 id 限权（线程 id 运行时才知），
  故在工具调用边界做线程级裁决——对 `/workspace/nl2sql_process_data/{thread}/...`
  的读，只放行 `{thread} == 当前会话线程 id`；其它线程目录一律拒绝。

会话线程 id 与 `langfuse_span._thread_id` 同源（metadata.langfuse_session_id →
trace_parent_thread_id → configurable.thread_id → execution_info.thread_id），保证
「自有目录」判定与 process_data 落盘目录（query_result_offload._write_full_table、
langfuse_span._dump_process_data）完全一致——子 agent 自己的产物、其它会话的产物
用同一把尺子区分。

仅读工具生效（read_file/ls/glob/grep）；write_file/edit_file 不受线程护栏约束
（写权限仍由静态规则收口，且子 agent 本就只能写活跃工作区）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

_logger = logging.getLogger(__name__)

# 受线程护栏约束的只读文件工具（deepagents FilesystemMiddleware 内置工具名）
_READ_TOOLS = ("read_file", "ls", "glob", "grep")

# process_data VFS 前缀（与 langfuse_span.VFS_PROCESS_DATA_PREFIX 同值）
_PROCESS_DATA_PREFIX = "/workspace/nl2sql_process_data/"


def _tool_name(request: ToolCallRequest) -> str:
    tc = getattr(request, "tool_call", None) or {}
    if isinstance(tc, dict):
        return str(tc.get("name", "") or "")
    return str(getattr(tc, "name", "") or "")


def _tool_args(request: ToolCallRequest) -> dict:
    tc = getattr(request, "tool_call", None) or {}
    args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
    return args if isinstance(args, dict) else {}


def _norm(path: str) -> str:
    """归一化 VFS 路径：反斜杠→正斜杠、折叠连续斜杠、去首尾空白。"""
    p = str(path or "").replace("\\", "/").strip()
    while "//" in p:
        p = p.replace("//", "/")
    return p


def _process_data_thread(path: str) -> str | None:
    """若 path 位于 process_data 下，返回其线程段（首段）；否则 None。

    返回空串表示「未指定线程」（如 ls 顶层 `/workspace/nl2sql_process_data/`）。
    """
    p = _norm(path)
    if not p.startswith(_PROCESS_DATA_PREFIX):
        return None
    rest = p[len(_PROCESS_DATA_PREFIX):]
    if not rest or rest == "/":
        return ""
    return rest.split("/", 1)[0]


def _candidate_paths(tool_name: str, args: dict) -> list[str]:
    """从工具参数中提取「目标路径」候选列表（用于线程护栏判定）。"""
    if tool_name == "read_file":
        return [str(args.get("file_path") or "")]
    if tool_name == "ls":
        return [str(args.get("path") or "")]
    if tool_name == "grep":
        # path 可缺省（缺省搜整个后端根）；无 path 时不在这里裁决，由静态层结果过滤
        p = args.get("path")
        return [str(p)] if p else []
    if tool_name == "glob":
        # pattern 是主匹配表达式；path 是可选基目录。二者都要看。
        out = [str(args.get("pattern") or "")]
        p = args.get("path")
        if p:
            out.append(str(p))
        return out
    return []


class FilesystemThreadGuardMiddleware(AgentMiddleware):
    """在 nl2sql 子 agent 工具调用边界拦截跨线程 process_data 读。"""

    def _session_thread_id(self, request: ToolCallRequest) -> str:
        """会话线程 id，与 langfuse_span._thread_id 同源。"""
        try:
            from agent.middlewares.langfuse_span import _thread_id
            return _thread_id(request) or ""
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _deny(self, request: ToolCallRequest, path: str, own_thread: str) -> ToolMessage:
        tool_call_id = getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""
        _logger.warning(
            "[fs_thread_guard] 拦截跨线程 process_data 读 %s（own=%s）", path,
            (own_thread or "")[:8],
        )
        return ToolMessage(
            content=(
                "Error: 越权读取被拒绝。当前查询只允许读取本次会话自己的中间产物目录"
                f"（/workspace/nl2sql_process_data/{own_thread}/），不能读取其它会话"
                f"/其它 run 的中间产物（{path}）。请改读本次会话自己的目录；业务口径"
                "只准来自语义层 MCP 工具（get_instructions / recall_queries / "
                "get_all_knowledge），禁止翻找其它会话的 process_data。"
            ),
            name=_tool_name(request),
            tool_call_id=tool_call_id,
            status="error",
        )

    def _guard(self, request: ToolCallRequest, handler: Callable):
        """对只读文件工具做线程护栏裁决；命中越权返回 deny，否则放行。"""
        name = _tool_name(request)
        if name not in _READ_TOOLS:
            return handler(request)

        own_thread = self._session_thread_id(request)
        # 线程 id 解析不到（单测/无 config 场景）→ fail-open 放行，由静态层兜底
        if not own_thread:
            return handler(request)

        for path in _candidate_paths(name, _tool_args(request)):
            if not path:
                continue
            thread = _process_data_thread(path)
            if thread is None:
                continue  # 不在 process_data 下，交由静态层裁决
            if thread == "":
                continue  # ls 顶层目录（仅暴露线程名，无内容），放行
            if thread == own_thread:
                continue  # 自有目录，放行
            # 其它线程的 process_data（含 eval-subject / query_result / schema 等）→ 拒绝
            return self._deny(request, path, own_thread)
        return handler(request)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        return self._guard(request, handler)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        result = self._guard(request, handler)
        if hasattr(result, "__await__"):
            return await result
        return result
