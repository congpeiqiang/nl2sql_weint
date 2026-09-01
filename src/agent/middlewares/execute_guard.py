"""ExecuteGuardMiddleware — execute（shell）命令护栏。

问题背景：deepagents 的 LocalShellBackend 用 `subprocess.run(shell=True)` 把命令直接跑在
宿主机上。FilesystemPermission 只约束 ls/read_file/write_file/edit_file/glob/grep 文件
工具，**管不住 execute**——已实测：agent 用 `execute("rm /tmp/xxx.txt")` 删除了工作区
之外的文件，绕过了「共享只读 / 代码根禁读写」的文件权限。

本中间件在 `wrap_tool_call` / `awrap_tool_call` 层拦截 execute 工具，拒绝：
  1. 破坏性命令：rm / rmdir / del / erase / rd / deltree / format / fdisk / mkfs /
     shutdown / reboot / taskkill / pkill / kill / unlink 等（POSIX + Windows）
  2. 工作区外绝对路径引用（`EXECUTE_GUARD_STRICT=0` 时仅此规则关闭，默认开启）：
     命令中出现活跃工作区之外、且不属于共享区（/shared）或工作区 VFS 前缀（/workspace）
     的绝对路径（POSIX `/xxx`、Windows `X:\\xxx`、`../` 越界），如 `cat /etc/passwd`

被拒命令返回 `status="error"` 的 ToolMessage，LLM 看到中文原因后可改用文件工具或
工作区内相对路径，不会进入死循环。

定位：这是「护栏」而非「安全边界」——命令混淆（cmd /c del、变量拼接、编码）可绕过。
要彻底隔离，请换沙箱后端（见 agent/backends/sandbox_setup.py 的 OpenSandboxBackend）。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

_logger = logging.getLogger(__name__)

# 破坏性命令动词（POSIX + Windows cmd/PowerShell），词边界匹配。
# 注意 \bdel\b 不会误中 "delete"（del 后是 e 仍是词字符，无边界）；"model" 无 del 子串。
_DESTRUCTIVE_RE = re.compile(
    r"(?<![\w./-])("
    r"\brm\b|\brmdir\b|\bunlink\b|\bdeltree\b|\berase\b|\bdel\b|\brd\b|"
    r"\bformat\b|\bfdisk\b|\bmkfs(?:\.[a-z0-9]+)?\b|"
    r"\bshutdown\b|\breboot\b|\btaskkill\b|\bpkill\b|\bkill\b|"
    r"\bremove(?:-item|-file)?\b|\brmtree\b"
    r")",
    re.IGNORECASE,
)

# 提取命令中的绝对路径 / 越界引用
_PATH_TOKEN_RE = re.compile(
    r"(/(?:[^\s;|&<>\"'`()]*))"          # POSIX 绝对路径 /xxx
    r"|(~[\\/][^\s;|&<>\"'`()]*)"        # 家目录 ~/xxx（家目录在工作区外）
    r"|([A-Za-z]:\\[^\s;|&<>\"'`()]*)"   # Windows 盘符路径 X:\xxx
    r"|(\.\.[\\/][^\s;|&<>\"'`()]*)"     # 越界 ../ 或 ..\
)

# 工作区 VFS 前缀（shell 里不一定真实存在，但不属于危险越界，放行避免误伤）
_ALLOWED_PREFIXES = ("/shared", "/workspace")


def _iter_abs_paths(command: str):
    """迭代命令中出现的绝对路径/越界 token（含虚拟 /shared /workspace）。"""
    for m in _PATH_TOKEN_RE.finditer(command):
        for g in m.groups():
            if g:
                yield g


def _is_outside(token: str, workspace: Path, shared_root: Path) -> bool:
    """判断路径 token 是否落在允许范围之外（工作区 / 共享区 / VFS 前缀）。"""
    try:
        if token.startswith("../") or token.startswith("..\\") or token.startswith("~"):
            return True  # 越界或家目录，都在工作区外
        if re.match(r"[A-Za-z]:[\\/]", token):
            # Windows 盘符路径：必须落在工作区或共享区下
            p = Path(token).resolve()
            for base in (workspace.resolve(), shared_root.resolve()):
                try:
                    p.relative_to(base)
                    return False
                except ValueError:
                    continue
            return True
        if token.startswith("/"):
            if token.startswith(_ALLOWED_PREFIXES):
                return False  # /shared /workspace VFS 前缀，放行
            ws_posix = str(workspace.resolve()).replace("\\", "/").rstrip("/")
            if ws_posix and (token == ws_posix or token.startswith(ws_posix + "/")):
                return False
            return True
    except Exception:  # noqa: BLE001  # 解析失败按越界处理，宁可多拦
        return True
    return False


def _check_command(command: str, workspace: Path, shared_root: Path) -> str | None:
    """返回拒绝原因；None = 放行。"""
    if not command or not command.strip():
        return None
    if _DESTRUCTIVE_RE.search(command):
        return "execute 禁止破坏性命令（rm/del/format 等删除、系统级操作），请改用文件工具 write_file/edit_file，且仅限工作区"
    strict = os.getenv("EXECUTE_GUARD_STRICT", "1") != "0"
    if strict:
        for token in _iter_abs_paths(command):
            if _is_outside(token, workspace, shared_root):
                return f"execute 引用了工作区外的路径: {token}，请只访问工作区（前端选中的工作区）或共享区（/shared）"
    return None


class ExecuteGuardMiddleware(AgentMiddleware):
    """拦截 execute 工具：拒绝破坏性命令与工作区外路径引用。"""

    def _resolve_roots(self) -> tuple[Path, Path]:
        from agent.workspace_manager import get_workspace_manager

        wm = get_workspace_manager()
        return wm.active_workspace, wm.shared_data_root

    @staticmethod
    def _tool_name(request: ToolCallRequest) -> str:
        tc = getattr(request, "tool_call", None) or {}
        if isinstance(tc, dict):
            return tc.get("name", "")
        return getattr(tc, "name", "")

    @staticmethod
    def _command(request: ToolCallRequest) -> str:
        tc = getattr(request, "tool_call", None) or {}
        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
        if isinstance(args, dict):
            cmd = args.get("command", "")
            return cmd if isinstance(cmd, str) else ""
        return ""

    def _deny(self, request: ToolCallRequest, reason: str) -> ToolMessage:
        tool_call_id = getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""
        cmd = self._command(request)
        _logger.warning(
            "[ExecuteGuard] 拦截 execute（%s）command=%s", reason, cmd[:200]
        )
        return ToolMessage(
            content=(
                "Error: execute 已被安全护栏拦截——" + reason + "。"
                "请改用文件工具（read_file / write_file / edit_file）只读写工作区，"
                "或使用工作区内的相对路径。"
            ),
            name="execute",
            tool_call_id=tool_call_id,
            status="error",
        )

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """同步工具调用：execute 先过护栏，其余透传。"""
        if self._tool_name(request) == "execute":
            cmd = self._command(request)
            if cmd:
                ws, shared = self._resolve_roots()
                reason = _check_command(cmd, ws, shared)
                if reason:
                    return self._deny(request, reason)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        """异步工具调用：execute 先过护栏，其余透传。"""
        if self._tool_name(request) == "execute":
            cmd = self._command(request)
            if cmd:
                ws, shared = self._resolve_roots()
                reason = _check_command(cmd, ws, shared)
                if reason:
                    return self._deny(request, reason)
        result = handler(request)
        if hasattr(result, "__await__"):
            return await result
        return result
