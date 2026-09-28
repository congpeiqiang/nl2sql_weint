# -*- coding: utf-8 -*-
"""execute 产出的图表文件 → 归属登记（P1-17，P1-3 的残留口）。

**问题**：`chart-saver` skill 通过 `execute` 调 `scripts/save_chart.py` 落盘 `.svg/.png`，
文件落在**全站共享**的 `report/` 目录，而这条路**没有身份**——子进程拿不到请求身份
（`DynamicLocalShellBackend._env` 是 import 时的 `os.environ.copy()`，往里注入身份得改
deepagents 内部）。于是 P1-3 之后仍是：`build_report` 产出的 `.md/.html` 有归属，
**同一个目录里** skill 产出的图无归属 → `can_read_report` 的无记录分支放行 → 任何登录
用户知道文件名就能读（`GET /api/reports/<name>?download=1`）。
"文件名不可猜"（基名_时间戳_4位随机）只是把问题缩成"从哪儿知道这个名字"，它不是授权。

**改法**：在中间件里做——这里一定有身份。`save_chart.py` 的 stdout 契约是一行
`✅ 图表已保存: <宿主绝对路径>`（脚本与 SKILL.md 都以它为准），结果侧把它读出来 →
`record_report_owner(基名, user_id, thread_id)`。**不要**试图给 shell 子进程注入身份。

**只认这一个显式标记，不扫"结果里出现的所有 report/ 路径"**：shell 结果里出现某个路径
不等于"刚由我写出"（`cat`/`ls` 别人的文件也会出现），而 `record_report_owner` 是
INSERT OR IGNORE（首次写入者胜）——对**无记录的存量文件**，一次 `cat` 就等于认领，
反过来把原主挡在外面。宁可漏登记（漏了 = 维持现状的放行），不可错登记。

身份取 `auth.runtime.caller_identity()`，再用 `ownership.is_real_owner` 判一层：内部调用与
dev 旁路的身份是 `"internal"` / `"dev"`（**非空**哨兵），照字面登记会把图记到它们名下 →
`can_read_report` 对"有记录且非本人"是拒绝 → 反而把真实用户锁在自己刚出的图外面。
非真实身份一律不登记（文件保持无记录 = 放行口径，与 `can_read_report` 的存量兼容一致）。
登记 best-effort：失败只记日志，绝不影响工具结果回给模型。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

_logger = logging.getLogger(__name__)

# 只看 `execute`：`save_chart.py`（chart-saver skill）与 report-export 的脚本都走它
_SHELL_TOOL = "execute"

# 契约来自 skills/main/chart-saver/scripts/save_chart.py 的 print 与 SKILL.md 的 Step 3。
# 允许全角/半角冒号与冒号后的空格差异；路径取到空白/引号/尖括号/中文标点为止（Windows
# 路径含 `\`、盘符含 `:`，所以不能按冒号断）。
_SAVED_RE = re.compile(r"✅\s*图表已保存\s*[:：]\s*(?P<path>[^\s\"'<>，。]+)")

# token 两侧/尾部的装饰字符。命令行回显、日志行尾、Markdown 反引号包裹都可能带这些，
# 而**漏登记**（图形同没登记 = 维持放行）比**错登记**严重，所以这里只"剥"不"弃"。
_TRIM = "`'\"()[]{}<>.,;:!?。，；：！？、）】》”’"


def _tool_name(request: ToolCallRequest) -> str:
    tc = getattr(request, "tool_call", None) or {}
    if isinstance(tc, dict):
        return tc.get("name", "")
    return getattr(tc, "name", "")


def _result_text(content: Any) -> str:
    """ToolMessage.content 可能是 str，也可能是 content blocks 列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _accept(raw_path: str) -> str | None:
    """把标记里的路径收敛成**该登记的文件名**；不合格返回 None。

    两道收敛，都是为了"只登记 report/ 里真实存在的那份"：
      · 父目录必须是**当前活跃工作区**的 report 目录 —— `save_chart.py` 支持 `--dir`
        落别处，而 `record_report_owner` 只认基名，别处的同名文件会在 report/ 里
        撞名（一旦撞上就变成"我把别人的文件认领了"）；
      · 文件必须真实存在 —— 排除"路径被截断/被 shell 加料"后凑出来的假名字。
    `Path.name` 同时认 Windows 与 POSIX 分隔符，脚本回退打 VFS 路径（`/workspace/report/x.svg`）
    时也能取到基名（但那种形态的父目录对不上 → 不登记，与不登记＝放行口径一致）。
    """
    raw = raw_path.strip().strip(_TRIM)
    if not raw:
        return None
    name = Path(raw).name
    if not name:
        return None
    try:
        from agent.workspace_manager import get_workspace_manager

        report_dir = get_workspace_manager().report_dir
        if os.path.normcase(str(Path(raw).parent.resolve())) != os.path.normcase(
            str(Path(report_dir).resolve())
        ):
            _logger.debug("[chart-owner] 路径不在 report 目录内，跳过登记: %s", raw)
            return None
        if not (report_dir / name).is_file():
            _logger.debug("[chart-owner] 文件不存在，跳过登记: %s", raw)
            return None
    except Exception:  # noqa: BLE001  解析不了就不登记（宁可维持放行，不可错登记）
        _logger.debug("[chart-owner] 归属路径解析失败，跳过登记: %s", raw, exc_info=True)
        return None
    return name


def saved_chart_names(text: str) -> list[str]:
    """从 execute 结果文本里取出**刚落盘的图表文件名**（去重、保序）。"""
    names: list[str] = []
    for m in _SAVED_RE.finditer(text or ""):
        name = _accept(m.group("path"))
        if name and name not in names:
            names.append(name)
    return names


def _register(names: list[str]) -> list[str]:
    """把文件名登记给当前登录身份；返回真正登记的（无身份/失败 → 空）。

    身份用 `is_real_owner` 判，**不是** `if uid`：内部调用与 dev 旁路的身份是
    `"internal"` / `"dev"` 这两个**非空**哨兵。照字面登记会把图表记到它们名下，
    而 `can_read_report` 对"有记录且不是本人"是拒绝 —— 那就等于**把真实用户锁在
    自己刚出的图外面**，比原来的洞更坏。非真实身份一律不登记 = 维持放行口径。
    """
    if not names:
        return []
    from agent.auth.ownership import is_real_owner
    from agent.auth.runtime import caller_identity

    uid = caller_identity()
    if not is_real_owner(uid):
        _logger.debug(
            "[chart-owner] 非真实登录身份（内部调用/dev 旁路：%r），跳过登记: %s", uid, names
        )
        return []
    try:
        from langgraph.config import get_config

        tid = str((get_config().get("configurable", {}) or {}).get("thread_id") or "")
    except Exception:  # noqa: BLE001  没有 config 上下文（离线/测试）→ 只丢 thread_id
        tid = ""
    try:
        from agent.auth.grants import record_report_owner

        for name in names:
            record_report_owner(name, uid, tid)
        return names
    except Exception:  # noqa: BLE001  账本问题不该影响出图
        _logger.warning("[chart-owner] 图表归属登记失败: %s", names, exc_info=True)
        return []


class ChartArtifactOwnerMiddleware(AgentMiddleware):
    """结果侧中间件：`execute` 返回里出现 `✅ 图表已保存: <path>` 就登记归属。

    挂在 `report/` 的所有写入路径上很关键：只要有一条产出图表的路径没登记，
    `can_read_report` 的无记录分支就会把它放行给全站（这正是 P1-17 要关的口子）。
    """

    def _after(self, request: ToolCallRequest, result: Any) -> Any:
        if _tool_name(request) != _SHELL_TOOL:
            return result
        content = getattr(result, "content", None)
        if content is None:
            return result
        names = saved_chart_names(_result_text(content))
        if names and _register(names):
            _logger.info("[chart-owner] 图表归属已登记: %s", names)
        return result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        return self._after(request, handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        result = handler(request)
        if hasattr(result, "__await__"):
            result = await result
        return self._after(request, result)
