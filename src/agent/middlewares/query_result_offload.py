"""QueryResultOffloadMiddleware — 大结果表在 run_sql 边界确定性落盘 + 消息瘦身。

动机（生产故障，会话 `01a064e1`「有多少部门，每个部门人数」）：nl2sql 子 agent
拿到 431 行 / 42.5KB 的 run_sql 结果后，在最终回复里把整张表重新生成了一遍
（markdown），单次超长生成撞上模型 60s 超时 ×3 重试 = 228s 静默（前端进度条冻结）。

关键认知：让模型「自己把全表写文件」行不通——`write_file(content=431行)` 仍要
模型输出 431 行 tokens，照样撞 60s。**正确治本 = 在 run_sql 工具边界由代码把
columns+rows 转成全量 markdown 表落盘（0 模型开销），并把进 state 的消息瘦身**成
`{columns, row_count, rows_truncated:true, full_result_file, rows:[前 N 样例]}`——
模型手里只有 20 行样例 + 文件指针，物理上不可能再全表重打；最终回复改为
「摘要 + Top20 样例 + 全量文件链接」（提示词侧配合，见 NL2SQL_SYSTEM_PROMPT.md）。

触发边界：仅当结果 **row_count > QUERY_RESULT_OFFLOAD_ROWS（默认 50）** 或
**文本 > LARGE_RESULT_TRUNCATE_CHARS（默认 8000）** 时落盘 + 瘦身；阈值以下小结果
行为不变（可完整贴表）。

顺序关键：必须注册在 `LangfuseSpanMiddleware` **之前（外层）**——langchain 把
middleware 列表按 first=outermost 组装（`factory._chain_tool_call_wrappers`：
Request 流 first→last→tool，Response 流 tool→last→first）。外层中间件在
handler(request) 返回**之后**做后处理，因此 LangfuseSpan 的 span output /
LLM-judge / process_data dump 仍吃原始全量 payload，只有进 state 的消息被瘦身
（与 main_agent 里 `MessageSlimmerMiddleware` 先于 LangfuseSpan 注册同构）。

安全：全程 fail-open——任何异常只记日志并返回原结果，绝不阻断 agent 循环。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

_logger = logging.getLogger(__name__)

# 大结果落盘 + 瘦身触发阈值（沿用既有 LARGE_RESULT_TRUNCATE_CHARS 口径，见 message_slimmer）
_ROWS_THRESHOLD_DEFAULT = int(os.environ.get("QUERY_RESULT_OFFLOAD_ROWS", "50"))
_CHARS_THRESHOLD_DEFAULT = int(os.environ.get("LARGE_RESULT_TRUNCATE_CHARS", "8000"))
# 进 state 的消息保留的样例行数（模型可见的 Top N）
_PREVIEW_ROWS_DEFAULT = int(os.environ.get("QUERY_RESULT_OFFLOAD_PREVIEW", "20"))
# 落盘 md 文件最多保留的行数（防超大结果撑爆磁盘；行数 > 该值只存前 N 行并注明）
_FILE_MAX_ROWS_DEFAULT = int(os.environ.get("QUERY_RESULT_OFFLOAD_FILE_MAX_ROWS", "10000"))

# 瘦身消息里给模型的行为指引（会随 JSON 一起进上下文）
_NOTE = (
    "该结果表过大（共 {total} 行），全量数据已由系统写入文件 {path}。"
    "**最终回复请遵守「大结果输出规则」**：只给①一句话结论/总数、②前 {preview} 行样例"
    "（本消息 rows 字段可见的数据）、③全量文件 VFS 路径。禁止把全量逐行重打、"
    "禁止用 read_file 读取该文件后逐行照抄到回复里。"
)


def _content_str(result: Any) -> str:
    """消息 content 归一化为纯文本（兼容 str / list[content-block] / dict）。"""
    raw = getattr(result, "content", None)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for it in raw:
            if isinstance(it, dict):
                if it.get("type") == "text":
                    parts.append(str(it.get("text", "")))
                else:
                    parts.append(str(it))
            else:
                parts.append(str(it))
        return "\n".join(parts)
    if raw is None:
        return ""
    return str(raw)


def _parse_json_dict(text: str) -> dict | None:
    """把 run_sql 结果文本解析为 dict；非 JSON 或非 dict 返回 None。"""
    s = (text or "").strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _int_or(value: Any, default: int) -> int:
    """安全取整数；非法值回退 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# 进 state 的样例行里单个单元格最大长度（防「行数少但单格超大」把预览撑爆）
_PREVIEW_CELL_MAX = 300


def _cap_cell(value: Any) -> Any:
    """预览单元格超长截断（全量仍在落盘 md 文件里，仅缩窄模型可见样例）。"""
    if isinstance(value, str) and len(value) > _PREVIEW_CELL_MAX:
        return value[:_PREVIEW_CELL_MAX] + f"…[截断, 全文见落盘文件]"
    return value


def _cap_preview_row(row: Any) -> Any:
    """把预览行的超长单元格截断（兼容 records dict 与 list 行）。"""
    if isinstance(row, dict):
        return {k: _cap_cell(v) for k, v in row.items()}
    if isinstance(row, list):
        return [_cap_cell(v) for v in row]
    return _cap_cell(row)


def _esc_cell(v: Any) -> str:
    """md 表格单元格安全化：转义管道、折叠换行。"""
    if v is None:
        return ""
    s = str(v)
    s = s.replace("\\", "\\\\").replace("|", "\\|")
    s = s.replace("\r", " ").replace("\n", " ")
    return s


def _records_to_markdown(columns: list, rows: list, cap: int) -> tuple[str, bool]:
    """把 rows（records dict 或 list[list]）转成全量 markdown 表。

    返回 (md文本, 是否被 cap 截断)。行数超过 cap 时只保留前 cap 行并注明，
    防止超大结果把工作区磁盘/报告文件撑爆。
    """
    cols = [str(c) for c in columns]
    truncated = len(rows) > cap
    data = rows[:cap]
    lines = []
    if cols:
        lines.append("| " + " | ".join(_esc_cell(c) for c in cols) + " |")
        lines.append("|" + "|".join("---" for _ in cols) + "|")
    for row in data:
        if isinstance(row, dict):
            cells = [_esc_cell(row.get(c)) for c in cols]
        elif isinstance(row, (list, tuple)):
            cells = [_esc_cell(row[i]) if i < len(row) else "" for i in range(len(cols))]
        else:
            cells = [_esc_cell(row)]
        lines.append("| " + " | ".join(cells) + " |")
    md = "\n".join(lines)
    if truncated:
        md += (
            f"\n\n*（结果共 {len(rows)} 行，此文件仅保留前 {cap} 行；"
            f"完整结果请回查数据库 / 重跑 SQL 获取）*"
        )
    return md, truncated


class QueryResultOffloadMiddleware(AgentMiddleware):
    """run_sql 大结果确定性落盘 + 消息瘦身（治本：防 228s 超长生成撞 60s 超时）。"""

    def __init__(
        self,
        *,
        rows_threshold: int = _ROWS_THRESHOLD_DEFAULT,
        max_chars: int = _CHARS_THRESHOLD_DEFAULT,
        preview_rows: int = _PREVIEW_ROWS_DEFAULT,
        file_max_rows: int = _FILE_MAX_ROWS_DEFAULT,
    ) -> None:
        self._rows_threshold = rows_threshold
        self._max_chars = max_chars
        self._preview_rows = max(1, preview_rows)
        self._file_max_rows = max(1, file_max_rows)

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步工具调用：先执行，再对 run_sql 大结果落盘 + 瘦身。"""
        try:
            result = handler(request)
        except Exception as e:  # noqa: BLE001  # 瘦身失败不阻断 agent，原始异常照常向上抛
            _logger.warning("[QueryResultOffload] wrap_tool_call 异常，按原始行为透传: %s", e)
            raise
        return self._offload_result(result, request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """异步工具调用：先执行，再对 run_sql 大结果落盘 + 瘦身。"""
        try:
            result = await handler(request)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[QueryResultOffload] awrap_tool_call 异常，按原始行为透传: %s", e)
            raise
        return self._offload_result(result, request)

    # ── 核心处理 ──────────────────────────────────────────────

    def _offload_result(self, result: Any, request: Any) -> Any:
        """只处理 run_sql 类工具的 ToolMessage；其余原样返回。"""
        if isinstance(result, ToolMessage):
            tool_name = getattr(result, "name", "") or ""
            if isinstance(tool_name, str) and tool_name.endswith("run_sql"):
                return self._maybe_offload(result, request)
        # Command / 其它类型（审批闸门 interrupt 等）不处理
        return result

    def _maybe_offload(self, message: ToolMessage, request: Any) -> ToolMessage:
        """解析 run_sql JSON；超阈值则落盘全量表 + 返回瘦身 ToolMessage。"""
        try:
            text = _content_str(message)
            if not text:
                return message
            parsed = _parse_json_dict(text)
            if parsed is None:
                return message
            columns = parsed.get("columns")
            rows = parsed.get("rows")
            if not isinstance(columns, list) or not isinstance(rows, list) or not columns:
                return message  # 非标准结构化结果不处理
            total = _int_or(parsed.get("row_count"), len(rows))
            if total < len(rows):
                total = len(rows)
            if not (total > self._rows_threshold or len(text) > self._max_chars):
                return message  # 阈值以下小结果：行为不变
            if not rows:
                return message  # 没有数据可落盘（空结果本身很小，无需瘦身）

            vfs_path = self._write_full_table(request, columns, rows, total)
            if not vfs_path:
                return message  # 落盘失败 → fail-open 保留原结果

            preview = [_cap_preview_row(r) for r in rows[: self._preview_rows]]
            slim = dict(parsed)  # 保留原结构其它字段（statement_count/statements 等）
            slim["columns"] = columns
            slim["row_count"] = total
            slim["rows"] = preview
            slim["rows_truncated"] = True
            slim["full_result_file"] = vfs_path
            slim["note"] = _NOTE.format(
                total=total, path=vfs_path, preview=self._preview_rows
            )
            replacement = json.dumps(slim, ensure_ascii=False, default=str)
            _logger.info(
                "[QueryResultOffload] %s 大结果落盘+瘦身: %d 行 / %d chars → %s"
                "（消息内仅保留前 %d 行样例）",
                message.name, total, len(text), vfs_path, len(preview),
            )
            return message.model_copy(update={"content": replacement})
        except Exception as e:  # noqa: BLE001  # fail-open：任何异常保留原结果
            _logger.warning("[QueryResultOffload] 处理失败，保留原结果: %s", e)
            return message

    # ── 落盘 ──────────────────────────────────────────────────

    def _write_full_table(
        self, request: Any, columns: list, rows: list, total: int
    ) -> str:
        """把全量 rows 转 markdown 写到进程数据目录，返回 VFS 路径。

        目标目录与 langfuse_span._dump_process_data 同源（同会话线程 id 组织）：
        `{active_workspace}/nl2sql_process_data/{session_thread_id}/query_result/{file}.md`
        （disk 直接写；VFS 视角 `/workspace/nl2sql_process_data/...` 经 composite
        backend `/workspace/` 路由可 read_file 读到同一文件）。
        """
        from agent.middlewares.langfuse_span import (
            _active_workspace_path,
            _question_id,
            _thread_id,
        )

        root = _active_workspace_path()
        if not root:
            return ""
        thread_id = _thread_id(request)
        if not thread_id:
            thread_id = "unknown_thread"
        qdir = Path(root) / "nl2sql_process_data" / thread_id / "query_result"
        qdir.mkdir(parents=True, exist_ok=True)
        md, capped = _records_to_markdown(columns, rows, self._file_max_rows)
        md_body = [
            f"# 查询结果全量表（row_count = {total}）",
            "",
            f"- 行数: {total}",
            f"- 列: {', '.join(str(c) for c in columns)}",
        ]
        # 行数说明（capped 时文件可能少于 total，注明口径）
        if capped:
            md_body.append(f"- 注: 文件仅存前 {self._file_max_rows} 行（共 {len(rows)} 行返回）")
        md_body += ["", "## 数据表", "", md]
        blob = "\n".join(md_body) + "\n"

        # seq：目录内已有 md 数 + 1（与 _dump_process_data 同约定）
        try:
            seq = len([f for f in qdir.iterdir() if f.suffix == ".md"]) + 1
        except OSError:
            seq = 1
        qprefix = (_question_id() or "")[:8]
        fname = f"{qprefix}_result-{seq}.md" if qprefix else f"result-{seq}.md"
        try:
            (qdir / fname).write_text(blob, encoding="utf-8")
        except OSError as e:
            _logger.warning("[QueryResultOffload] 全量表写盘失败: %s", e)
            return ""
        return f"/workspace/nl2sql_process_data/{thread_id}/query_result/{fname}"
