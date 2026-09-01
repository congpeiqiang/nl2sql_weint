# -*- coding: utf-8 -*-
"""SQL 只读硬拦截（原 P1-3 审批闸门升级，2026-08-28）。

在 nl2sql 子 agent 的 run_sql 类工具执行前做确定性分类：
- 只读查询（SELECT/WITH...SELECT/SHOW/DESCRIBE/EXPLAIN/PRAGMA...）→ 直接放行（含全表拉取）；
- 写/DDL（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE...）→ **直接拒绝执行**，
  返回 ``status="error"`` 的 ToolMessage，不执行、不弹人工审批卡。

历史（v1）：曾用 langchain 1.x ``HumanInTheLoopMiddleware``（after_model 阶段 interrupt）做
写/DDL 人工审批，前端 ToolApprovalInterrupt + 后端 ``POST /api/threads/{tid}/sql-approval`` 恢复。
2026-08-28 产品要求「执行的 SQL 只运行查询操作，其余操作一律禁止」→ 升级为硬拦截，
写/DDL 无审批通道；``configurable.sql_approval_policy``（前端「SQL 审批」开关）不再影响写/DDL。
``classify_sql`` 保留（eval 复用 + 前端只读展示）。API 端点 ``api/sql_approval.py`` 不再被触发，
保留不动（避免破坏旧协议）。
"""
import logging
import re
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

_logger = logging.getLogger(__name__)

# ── SQL 分类 ────────────────────────────────────────────────────────────

# 首个关键词属于该集合 → 写/DDL 操作
_WRITE_LEADING = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "MERGE", "GRANT", "REVOKE", "RENAME", "ATTACH", "DETACH",
    "COMMENT", "VACUUM", "OPTIMIZE", "CALL", "EXEC", "EXECUTE",
    "SET", "USE", "LOAD", "COPY",
}

# 首个关键词属于该集合 → 只读家族（SELECT 另行做全表拉取检查）
_READ_LEADING = {
    "SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN",
    "PRAGMA", "LIST", "HELP",
}

# 聚合函数出现 → 即使无 WHERE/LIMIT 也不算全表拉取
_AGGREGATE_RE = re.compile(
    r"\b(COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT|ARRAY_AGG|APPROX_COUNT_DISTINCT)\s*\(",
    re.IGNORECASE,
)

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def _strip_sql(sql: str) -> str:
    """去掉注释与字符串字面量（防关键词误判），仅保留结构。"""
    s = _BLOCK_COMMENT_RE.sub(" ", sql)
    s = _LINE_COMMENT_RE.sub(" ", s)
    s = _STRING_RE.sub("''", s)
    return s


def _first_keyword(stmt: str) -> str:
    m = re.match(r"\s*\(*\s*([A-Za-z]+)", stmt)
    return m.group(1).upper() if m else ""


def classify_sql(sql: str) -> tuple[str, str]:
    """对 SQL 做闸门分类。

    Returns:
        (verdict, detail)：
        - ("read", "")              只读查询，放行
        - ("write", <关键词>)       写/DDL，直接拒绝
        - ("full_dump", "")         疑似全表拉取（无 WHERE/LIMIT/聚合），放行
    """
    if not sql or not sql.strip():
        return "read", ""

    stripped = _strip_sql(sql)
    # 多语句：按顶层分号拆分，取最严结论（write > full_dump > read）
    statements = [s for s in (seg.strip() for seg in stripped.split(";")) if s]
    if not statements:
        return "read", ""

    verdict = "read"
    detail = ""
    for stmt in statements:
        kw = _first_keyword(stmt)
        if kw in _WRITE_LEADING:
            return "write", kw
        if kw == "WITH":
            # CTE 后可能是 INSERT/UPDATE/DELETE（WITH ... INSERT INTO ...）
            m = re.search(
                r"\b(INSERT|UPDATE|DELETE|REPLACE|MERGE)\b", stmt, re.IGNORECASE
            )
            if m:
                return "write", m.group(1).upper()
            kw = "SELECT"  # WITH ... SELECT 按 SELECT 规则检查
        if kw not in _READ_LEADING and kw != "SELECT":
            # 无法识别的语句一律按写操作处理（保守拦截）
            return "write", kw or "UNKNOWN"
        if kw == "SELECT":
            upper = stmt.upper()
            has_limit = re.search(r"\bLIMIT\b", upper) is not None
            has_where = re.search(r"\bWHERE\b", upper) is not None
            has_group = re.search(r"\b(GROUP\s+BY|HAVING)\b", upper) is not None
            has_agg = _AGGREGATE_RE.search(stmt) is not None
            if not (has_limit or has_where or has_group or has_agg):
                verdict = "full_dump"
    return verdict, detail


# ── 只读硬拦截中间件 ────────────────────────────────────────────────────

# run_sql 类工具名特征：wrenai_<库名>_run_sql / dbmcp_run_sql / run_sql
def _is_run_sql_tool(name: str) -> bool:
    return name == "run_sql" or name.endswith("_run_sql")


class SqlReadOnlyMiddleware(AgentMiddleware):
    """只读硬拦截：run_sql 类工具执行前，非 SELECT（写/DDL）直接拒绝，不执行、不审批。"""

    @staticmethod
    def _tool_name(request: ToolCallRequest) -> str:
        tc = getattr(request, "tool_call", None) or {}
        if isinstance(tc, dict):
            return tc.get("name", "")
        return getattr(tc, "name", "")

    @staticmethod
    def _sql(request: ToolCallRequest) -> str:
        tc = getattr(request, "tool_call", None) or {}
        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
        if isinstance(args, dict):
            sql = args.get("sql", "")
            return sql if isinstance(sql, str) else ""
        return ""

    def _deny(self, request: ToolCallRequest, detail: str) -> ToolMessage:
        tool_call_id = getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""
        sql = self._sql(request)
        _logger.warning(
            "[sql_approval] 拦截非 SELECT SQL（%s）: %s", detail, sql[:200]
        )
        return ToolMessage(
            content=(
                "Error: 系统为只读查询系统，仅允许执行 SELECT（含 WITH...SELECT）只读查询。"
                f"检测到 {detail or '非查询'} 操作，已禁止执行。"
                "请改用 SELECT 查询，或向用户说明无法执行数据修改。"
            ),
            name=self._tool_name(request),
            tool_call_id=tool_call_id,
            status="error",
        )

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """同步工具调用：run_sql 先做只读校验，写/DDL 直接拒绝。"""
        if _is_run_sql_tool(self._tool_name(request)):
            sql = self._sql(request)
            if sql:
                verdict, detail = classify_sql(sql)
                if verdict == "write":
                    return self._deny(request, detail)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        """异步工具调用：run_sql 先做只读校验，写/DDL 直接拒绝。"""
        if _is_run_sql_tool(self._tool_name(request)):
            sql = self._sql(request)
            if sql:
                verdict, detail = classify_sql(sql)
                if verdict == "write":
                    return self._deny(request, detail)
        result = handler(request)
        if hasattr(result, "__await__"):
            return await result
        return result


def build_sql_approval_middleware(tools):
    """为 run_sql 类工具构建只读硬拦截中间件。

    Args:
        tools: 子 agent 解析后的工具列表（仅用于确认 run_sql 工具存在并记日志）。

    Returns:
        SqlReadOnlyMiddleware 实例；无 run_sql 工具时返回 None。
    """
    run_sql_names = [
        getattr(t, "name", "") for t in tools
        if _is_run_sql_tool(getattr(t, "name", "") or "")
    ]
    if not run_sql_names:
        _logger.info("[sql_approval] 未发现 run_sql 工具，跳过只读闸门")
        return None
    _logger.info("[sql_approval] 只读硬拦截挂载: %s", ", ".join(sorted(run_sql_names)))
    return SqlReadOnlyMiddleware()
