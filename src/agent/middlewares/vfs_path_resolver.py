"""VfsPathResolverMiddleware — 把模型输出里的 VFS 路径改写为真实磁盘路径。

问题：agent 全程在 VFS 命名空间工作（`/workspace/` → 当前工作区、`/shared/memory/` → 共享
memory、`/shared/skills/` → 共享 skills）。技能提示词让 agent 在聊天里回显
「最终结果 → /workspace/report/<file>」，但 `/workspace/` 前缀对用户是误导：

- 不指示具体是哪个工作区（workspace1 / 默认 / …）；
- 不是可直接打开的真实文件系统路径。

本中间件在模型调用返回后（wrap_model_call / awrap_model_call 的后处理）把最终
AIMessage 文本里的 VFS 路径改写为真实磁盘路径（统一正斜杠，Windows 下可直接
导航/打开），让聊天显示 `D:/workspace1/report/xxx.html` 而不是
`/workspace/report/xxx.html`。

安全设计：
- 只改 AIMessage 文本，绝不碰 ToolMessage 与 tool_call 参数（write_file 的 path 参数
  必须保持 VFS 路径——DynamicFilesystemBackend._resolve_path 只按 '/' 前缀解析）。
- 负向前瞻排除真实磁盘路径（如 `D:/workspace/report/...`、`C:\\workspace/...` 前的
  分隔符），避免二次改写。
- fail-open：任何异常只记日志、返回原响应，绝不阻断 agent 循环。
- 幂等：改写后的路径不以 VFS 前缀开头，重复处理无副作用。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

# VFS 前缀 → WorkspaceManager 上解析真实目录的属性名（与 main_agent 的
# CompositeBackend routes 一一对应：/shared/memory/、/shared/skills/、/workspace/）。
_VFS_PREFIXES = {
    "/shared/memory/": "shared_memory_dir",
    "/shared/skills/": "shared_skills_dir",
    "/workspace/": "active_workspace",
}

# 匹配 VFS 路径。路径字符排除空白与中英文标点（路径后常跟着「（可悬停查看…）」等注解，
# 也常以逗号/句号/右括号收尾）。负向前瞻排除真实磁盘路径：
# `D:/workspace/...`、`C:\workspace/...` 里 `/workspace` 前的字符是路径分隔符。
_VFS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_:/\\])"
    r"/(?:workspace|shared/(?:memory|skills))"
    r"/[^\s,，。；;:：!！?？()（）\[\]{}<>\"'`、|]+"
)


def _forward(p: Path) -> str:
    """路径转正斜杠字符串（Windows 下可直接导航/打开）。"""
    return str(p).replace("\\", "/")


def _resolve_vfs_path(vpath: str, wm: Any) -> str:
    """把单个 VFS 路径映射为真实磁盘路径；非 VFS 路径原样返回。"""
    for prefix, attr in _VFS_PREFIXES.items():
        if vpath.startswith(prefix):
            root = getattr(wm, attr)
            return _forward(root / vpath[len(prefix):])
    return vpath


def _rewrite_text(text: str, wm: Any) -> str:
    """把文本中出现的 VFS 路径改写为真实磁盘路径。"""
    if "/workspace/" not in text and "/shared/" not in text:
        return text
    return _VFS_PATH_RE.sub(lambda m: _resolve_vfs_path(m.group(0), wm), text)


class VfsPathResolverMiddleware(AgentMiddleware):
    """模型输出后处理：AIMessage 文本里的 VFS 路径 → 真实磁盘路径。"""

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        return self._postprocess(handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        result = handler(request)
        if hasattr(result, "__await__"):
            result = await result
        return self._postprocess(result)

    def _postprocess(self, response: ModelResponse[ResponseT]) -> ModelResponse[ResponseT]:
        """改写 response.result 里所有 AIMessage 文本中的 VFS 路径。"""
        try:
            from agent.workspace_manager import get_workspace_manager

            messages = getattr(response, "result", None)
            if not isinstance(messages, list):
                return response
            wm = get_workspace_manager()
            for i, msg in enumerate(messages):
                if not isinstance(msg, AIMessage):
                    continue
                content = msg.content
                if not isinstance(content, str):
                    continue
                new_text = _rewrite_text(content, wm)
                if new_text != content:
                    messages[i] = msg.model_copy(update={"content": new_text})
        except Exception:  # noqa: BLE001
            _logger.warning("[vfs_path] 后处理失败，返回原响应", exc_info=True)
        return response
