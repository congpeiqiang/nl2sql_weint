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

# ── 语义层 run_sql「双重 LIMIT」防御性归一 ───────────────────────────
# Wren 的 run_sql 在服务端**无条件**追加 ``LIMIT {limit+1}``（_query_with_limit_probe
# 多取一行探测是否截断），SQL 若自带尾部 LIMIT 会变成 ``…LIMIT n\nLIMIT n+1``
# → MySQL 1064（如 `near 'LIMIT 1001' at line 2`）。
# 子 agent 各 skill 仍残留直连时代「始终加 LIMIT」旧指令（sql-of-thought 性能节、
# performance-optimization 规则 2、sql-validate 自动补 LIMIT），模型常自带 LIMIT。
# 故在工具边界做防御：剥掉**最外层尾部**整数 LIMIT 并折算进 limit 参数——
# 与 run_sql 的 `limit` 参数契约对齐（SQL 不带 LIMIT、由服务端追加）。
# 仅作用于语义层工具（wrenai_*_run_sql）；dbmcp 直连通道自身幂等
# （db_server._apply_default_limit：已含 LIMIT 跳过追加），无需处理。
# offset 形态（LIMIT o,c / LIMIT n OFFSET m）无法用单值 limit 表达 → 跳过不剥。
_TRAILING_INT_LIMIT_RE = re.compile(r"(?i)\bLIMIT\s+(\d+)\s*$")


def _strip_trailing_noise(sql: str) -> str:
    """去掉语句末尾的分号/注释，便于匹配尾部 LIMIT。

    注释起点前须为空白或串首（防误伤字符串字面量里的 ``--``/``/*``）。
    """
    s = sql.rstrip()
    prev = None
    while prev != s:
        prev = s
        if s.endswith(";"):
            s = s[:-1].rstrip()
            continue
        bm = re.search(r"/\*[\s\S]*?\*/\s*$", s)
        if bm and (bm.start() == 0 or s[bm.start() - 1].isspace()):
            s = s[: bm.start()].rstrip()
            continue
        lm = re.search(r"(?<=\s)--[^\r\n]*$", s)
        if lm:
            s = s[: lm.start()].rstrip()
            continue
    return s


def normalize_semantic_limit(sql: str, tool_limit: object = None) -> tuple[str, object]:
    """若 SQL 最外层以 ``LIMIT <int>`` 结尾，剥掉并折算进 limit。

    Returns:
        (new_sql, limit)：
        - 未命中尾部整数 LIMIT → (原 sql, 原 limit)，零改动；
        - 命中 → SQL 去掉该 LIMIT 子句；limit = min(SQL 自带 n, 显式 limit)，
          绝不超过二者任一意图（无显式 limit 时即取 n，top-N 语义保持）。
    """
    if not sql or not isinstance(sql, str):
        return sql, tool_limit
    s = _strip_trailing_noise(sql)
    if not s:
        return sql, tool_limit
    m = _TRAILING_INT_LIMIT_RE.search(s)
    if not m:
        return sql, tool_limit
    n = int(m.group(1))
    new_sql = s[: m.start()].rstrip()
    try:
        cap = int(tool_limit) if tool_limit not in (None, "") else None
    except (TypeError, ValueError):
        cap = None
    merged = n if cap is None else min(n, cap)
    return new_sql, merged


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

    @staticmethod
    def _tool_args(request: ToolCallRequest) -> dict | None:
        """返回 tool_call 的 args dict（供就地位改写）；非 dict 返回 None。"""
        tc = getattr(request, "tool_call", None) or {}
        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
        return args if isinstance(args, dict) else None

    @staticmethod
    def _normalize_wrenai_limit(request: ToolCallRequest) -> None:
        """wrenai 语义层 run_sql：SQL 尾部自带整数 LIMIT → 剥掉并折算进 limit。

        Wren 服务端会无条件追加 ``LIMIT {limit+1}``，SQL 自带尾部 LIMIT 即触发
        双重 LIMIT 语法错误。防御性改写调用参数（不执行、不弹窗，纯归一）。
        """
        name = SqlReadOnlyMiddleware._tool_name(request)
        if not (name.startswith("wrenai_") and name.endswith("_run_sql")):
            return
        args = SqlReadOnlyMiddleware._tool_args(request)
        if not args:
            return
        sql = args.get("sql", "")
        if not isinstance(sql, str) or not sql.strip():
            return
        new_sql, merged = normalize_semantic_limit(sql, args.get("limit"))
        if new_sql == sql:
            return
        args["sql"] = new_sql
        args["limit"] = merged
        _logger.info(
            "[sql_approval] %s: 归一化 SQL 尾部 LIMIT → limit=%s（防 Wren 双重 LIMIT 语法错误）",
            name, merged,
        )

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
            # 只读放行：wrenai 语义层 run_sql 剥尾部自带 LIMIT（防服务端双重追加）
            self._normalize_wrenai_limit(request)
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
            # 只读放行：wrenai 语义层 run_sql 剥尾部自带 LIMIT（防服务端双重追加）
            self._normalize_wrenai_limit(request)
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
