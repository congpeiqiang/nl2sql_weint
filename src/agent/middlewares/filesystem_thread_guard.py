"""FilesystemThreadGuardMiddleware — nl2sql 子 agent 文件读写的线程级护栏。

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
  `_check_fs_permission`）。它管不住 process_data 内部——那条 allow 是**全线程通配**，
  结果过滤只查静态权限、不认识线程归属。
- **本中间件（运行时层）**：在工具调用边界做线程级裁决，管三件事：

  1. **读**（read_file/ls/glob/grep）：`/workspace/nl2sql_process_data/{thread}/...`
     只放行 `{thread} == 当前会话线程 id`。
  2. **写**（write_file/edit_file）：只收紧「写向其它线程 process_data 子目录」，
     其余写行为维持静态层原有粒度（读别人=泄题，写别人=投毒污染）。
  3. **grep 的搜索范围**：未收窄（缺省 / `/workspace` / process_data 根）的 grep 必然
     把其它线程的命中带回来，且其结果过滤兜不住——故要求显式收窄到自有线程目录或
     `/shared`。`ls`/`glob` 无法这样在入参上收窄（pattern 本身就可能跨目录），改为对
     结果做后置过滤（顺带堵住「顶层 ls 枚举他人线程名 → 定向读写」的组合路径）。

会话线程 id 与 `langfuse_span._thread_id` 同源（metadata.langfuse_session_id →
trace_parent_thread_id → configurable.thread_id → execution_info.thread_id），保证
「自有目录」判定与 process_data 落盘目录（query_result_offload._write_full_table、
langfuse_span._dump_process_data）完全一致——子 agent 自己的产物、其它会话的产物
用同一把尺子区分。

**入参必须自归一**：本护栏在 `wrap_tool_call` 拿到的是模型**未归一化的原始入参**，而
工具侧先 `validate_path()` 再落盘（会给相对路径补前导斜杠：`workspace/x` →
`/workspace/x`）。不补同款归一，`read_file("workspace/nl2sql_process_data/<其它线程>/x")`
就会绕过前缀判定拿到内容（2026-09-18 实测确认的既有绕过）。
"""
from __future__ import annotations

import ast
import logging
import posixpath
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

_logger = logging.getLogger(__name__)

# 受线程护栏约束的文件工具（deepagents FilesystemMiddleware 内置工具名）
_READ_TOOLS = ("read_file", "ls", "glob", "grep")
_WRITE_TOOLS = ("write_file", "edit_file")

# process_data VFS 前缀（与 langfuse_span.VFS_PROCESS_DATA_PREFIX 同值）
_PROCESS_DATA_PREFIX = "/workspace/nl2sql_process_data/"
_PROCESS_DATA_ROOT = "/workspace/nl2sql_process_data"


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
    """归一化 VFS 路径：反斜杠→正斜杠、去首尾空白、折叠 `.` 与重复斜杠。

    这里必须与工具侧的 `validate_path` 同构（后者 `os.path.normpath`，再补前导斜杠）。
    先自己折叠 `//` 再走 posixpath：posixpath 会**保留**恰好两个前导斜杠（POSIX 特例），
    `//workspace/...` 会绕过前缀判定。posixpath 而非 os.path —— 后者在 Windows 上吐
    反斜杠。
    """
    p = str(path or "").replace("\\", "/").strip()
    while "//" in p:
        p = p.replace("//", "/")
    if not p:
        return ""
    return posixpath.normpath(p)


def _to_vfs(path: str) -> str:
    """归一成 VFS 绝对形式（镜像 deepagents `validate_path` 的补斜杠行为）。

    工具侧先 `validate_path()` 再落盘，它把相对路径补成以 `/` 开头并把 `.`/`//` 折叠掉
    （`workspace/x` → `/workspace/x`、`a.json` → `/a.json`、`/workspace/.` → `/workspace`）；
    本护栏拿到的是原始入参，必须做同款归一，否则前缀判定形同虚设——2026-09-18 实测确认
    两条既有绕过：`workspace/nl2sql_process_data/<其它线程>/x`（缺前导斜杠）与
    `/workspace/./nl2sql_process_data/<其它线程>/x`（`.` 段）。`..` 不在此解析——工具侧
    validate_path 会直接抛错拒绝。
    """
    p = _norm(path)
    if p and not p.startswith("/"):
        p = "/" + p
    return p


def _process_data_thread(path: str) -> str | None:
    """若 path 位于 process_data 下，返回其线程段（首段）；否则 None。

    返回空串表示「未指定线程」（如 ls 顶层 `/workspace/nl2sql_process_data/`）。
    """
    p = _to_vfs(path)
    if p == _PROCESS_DATA_ROOT:  # 根自身（不带尾斜杠）也算「未指定线程」
        return ""
    if not p.startswith(_PROCESS_DATA_PREFIX):
        return None
    rest = p[len(_PROCESS_DATA_PREFIX):]
    if not rest or rest == "/":
        return ""
    return rest.split("/", 1)[0]


def _covers_process_data(target: str) -> bool:
    """被搜索/列出的目录是否**覆盖** process_data 根（即 process_data 位于其下）。

    用于 grep 的入参收窄：缺省（后端根）、`/`、`/workspace`、process_data 根自身都算覆盖。
    """
    t = _to_vfs(target).rstrip("/")
    if not t:
        return True  # 缺省 = 后端根
    if t == _PROCESS_DATA_ROOT or t.startswith(_PROCESS_DATA_PREFIX):
        return True
    # 更上层的目录（其下包含 process_data），如 /workspace
    return _PROCESS_DATA_PREFIX.startswith(t + "/")


def _candidate_paths(tool_name: str, args: dict) -> list[str]:
    """从工具参数中提取「目标路径」候选列表（用于线程护栏判定）。"""
    if tool_name == "read_file":
        return [str(args.get("file_path") or "")]
    if tool_name in _WRITE_TOOLS:
        return [str(args.get("file_path") or "")]
    if tool_name == "ls":
        return [str(args.get("path") or "")]
    if tool_name == "grep":
        # 未收窄的 grep 已在入参闸门拦掉；这里只处理显式给出了 path 的情况
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
    """在 nl2sql 子 agent 工具调用边界拦截跨线程 process_data 读写。"""

    def _session_thread_id(self, request: ToolCallRequest) -> str:
        """会话线程 id，与 langfuse_span._thread_id 同源。"""
        try:
            from agent.middlewares.langfuse_span import _thread_id
            return _thread_id(request) or ""
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ── deny 文案 ──────────────────────────────────────────────

    def _deny(self, request: ToolCallRequest, path: str, own_thread: str, *,
              write: bool = False) -> ToolMessage:
        tool_call_id = getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""
        _logger.warning(
            "[fs_thread_guard] 拦截跨线程 process_data %s %s（own=%s）",
            "写" if write else "读", path, (own_thread or "")[:8],
        )
        if write:
            content = (
                "Error: 越权写入被拒绝。当前查询只允许写入本次会话自己的中间产物目录"
                f"（/workspace/nl2sql_process_data/{own_thread}/），不能覆盖/污染其它"
                f"会话或其它 run 的中间产物（{path}）。请改写到本次会话自己的目录。"
            )
        else:
            content = (
                "Error: 越权读取被拒绝。当前查询只允许读取本次会话自己的中间产物目录"
                f"（/workspace/nl2sql_process_data/{own_thread}/），不能读取其它会话"
                f"/其它 run 的中间产物（{path}）。请改读本次会话自己的目录；业务口径"
                "只准来自语义层 MCP 工具（get_instructions / recall_queries / "
                "get_all_knowledge），禁止翻找其它会话的 process_data。"
            )
        return ToolMessage(
            content=content,
            name=_tool_name(request),
            tool_call_id=tool_call_id,
            status="error",
        )

    def _deny_scope(self, request: ToolCallRequest, target: str, own_thread: str) -> ToolMessage:
        """grep 的搜索范围未收窄 → 拒绝并指路。"""
        tool_call_id = getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""
        _logger.warning(
            "[fs_thread_guard] 拦截未收窄的 grep（target=%r, own=%s）",
            target or "<缺省>", (own_thread or "")[:8],
        )
        return ToolMessage(
            content=(
                "Error: 搜索范围被拒绝。未收窄的 grep（不传 path、或 path 为 /workspace 等"
                "上层目录）会连其它会话/其它 run 的中间产物一起搜，命中里会带上它们的"
                "内容片段（含 eval-subject 参考答案）。请显式把 path 收窄到本次会话自己的"
                f"目录（/workspace/nl2sql_process_data/{own_thread}/）或共享区（/shared/…）；"
                "业务口径只准来自语义层 MCP 工具（get_instructions / recall_queries / "
                "get_all_knowledge）。"
            ),
            name=_tool_name(request),
            tool_call_id=tool_call_id,
            status="error",
        )

    # ── 入参闸门 ────────────────────────────────────────────────

    def _precheck(self, request: ToolCallRequest) -> ToolMessage | None:
        """工具调用前的线程裁决；返回 ToolMessage 表示拒绝，None 表示放行。"""
        name = _tool_name(request)
        if name not in _READ_TOOLS and name not in _WRITE_TOOLS:
            return None

        own_thread = self._session_thread_id(request)
        # 线程 id 解析不到（单测/无 config 场景）→ fail-open 放行，由静态层兜底
        if not own_thread:
            return None

        args = _tool_args(request)

        # grep：搜索范围覆盖 process_data 且未落在自有线程目录内 → 拒搜。
        # （缺省 path、path="/"、path="/workspace"、path=process_data 根 都属此类；
        #  显式指出自有线程目录或 /shared 才放行。）
        if name == "grep":
            target = str(args.get("path") or "")
            thread = _process_data_thread(target)
            if thread is not None and thread not in ("", own_thread):
                # 明确指向别人线程的目录 → 用读越权文案（比"请收窄范围"更贴切）
                return self._deny(request, target, own_thread)
            if _covers_process_data(target) and thread != own_thread:
                return self._deny_scope(request, target, own_thread)

        for path in _candidate_paths(name, args):
            if not path:
                continue
            thread = _process_data_thread(path)
            if thread is None:
                continue  # 不在 process_data 下，交由静态层裁决
            if thread == own_thread:
                continue  # 自有目录，放行
            if thread == "":
                continue  # process_data 根自身（ls 顶层）——由结果后置过滤收口
            return self._deny(request, path, own_thread, write=name in _WRITE_TOOLS)
        return None

    # ── 结果后置过滤（ls / glob）───────────────────────────────

    @staticmethod
    def _is_foreign(path: str, own_thread: str) -> bool:
        """该结果项是否指向其它线程的 process_data。"""
        thread = _process_data_thread(path)
        return thread is not None and thread != "" and thread != own_thread

    def _postfilter(self, request: ToolCallRequest, result: Any) -> Any:
        """剔除 ls/glob 结果里他人线程的 process_data 项。

        返回体是 `str(list[str])`（deepagents 对 list 结果先截断成合法 list 再 `str()`，
        见 `truncate_if_too_long` 的 list 分支），故 `ast.literal_eval` 可精确解析、无截断
        残段问题。错误分支（`Error: ...`）与无法解析的内容一律**原样放行**——被截断/丢失
        的部分模型同样看不到，没有「不给就泄漏」的压力。
        """
        name = _tool_name(request)
        if name not in ("ls", "glob"):
            return result
        content = getattr(result, "content", None)
        if not isinstance(content, str) or not content.lstrip().startswith("["):
            return result
        try:
            items = ast.literal_eval(content.strip())
        except Exception:  # noqa: BLE001
            _logger.warning("[fs_thread_guard] %s 结果无法解析为 list，保留原结果", name)
            return result
        if not isinstance(items, list):
            return result

        own_thread = self._session_thread_id(request)
        if not own_thread:
            return result
        kept = [it for it in items if not self._is_foreign(str(it), own_thread)]
        if len(kept) == len(items):
            return result
        _logger.warning(
            "[fs_thread_guard] %s 结果过滤：剔除 %d 项他人线程 process_data（own=%s）",
            name, len(items) - len(kept), own_thread[:8],
        )
        new_content = str(kept)
        try:
            return result.model_copy(update={"content": new_content})
        except Exception:  # noqa: BLE001
            return ToolMessage(
                content=new_content,
                name=name,
                tool_call_id=getattr(getattr(request, "runtime", None), "tool_call_id", None) or "",
                status=getattr(result, "status", None) or "success",
            )

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        denied = self._precheck(request)
        if denied is not None:
            return denied
        return self._postfilter(request, handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        denied = self._precheck(request)
        if denied is not None:
            return denied
        result = handler(request)
        if hasattr(result, "__await__"):
            result = await result
        return self._postfilter(request, result)
