# -*- coding: utf-8 -*-
"""SQL 执行审批闸门（P1-3，对标 deepseek-harness interaction/user-approval）。

在 nl2sql 子 agent 的 run_sql 类工具执行前做确定性分类：
- 只读查询（SELECT/WITH...SELECT）→ 直接放行（含全表拉取，不再审批）；
- 写/DDL（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE...）→ interrupt 审批。

实现：langchain 1.x ``HumanInTheLoopMiddleware``（after_model 阶段 interrupt），
payload 为 ``{action_requests, review_configs}``——与前端 ToolApprovalInterrupt
组件的协议一致。用户决策（approve/edit/reject）经后端恢复端点
``POST /api/threads/{sub_thread_id}/sql-approval`` 回传，子 run 继续执行。

策略开关：前端 localStorage 持久化（ask/never），随 configurable.sql_approval_policy
透传（主 agent run → deepagents_async_config_patch → 子 agent run）。
"""
import logging
import re

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
        - ("write", <关键词>)       写/DDL，需审批
        - ("full_dump", "")         疑似全表拉取（无 WHERE/LIMIT/聚合），需审批
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


# ── HITL 中间件构建 ──────────────────────────────────────────────────────

# run_sql 类工具名特征：wrenai_<库名>_run_sql / dbmcp_run_sql / run_sql
def _is_run_sql_tool(name: str) -> bool:
    return name == "run_sql" or name.endswith("_run_sql")


def _make_when_predicate():
    """构建 `when` 判定：只读（含全表拉取）放行，仅写/DDL 触发 interrupt。

    策略开关：configurable.sql_approval_policy == "never" 时全部放行
    （前端「SQL 审批」关闭；默认 "ask"）。
    """

    def when(request) -> bool:  # noqa: ANN001  ToolCallRequest
        try:
            from langgraph.config import get_config

            configurable = (get_config().get("configurable", {}) or {})
            policy = str(configurable.get("sql_approval_policy", "ask")).lower()
        except Exception:  # noqa: BLE001  读不到配置按默认 ask
            policy = "ask"
        if policy == "never":
            return False

        args = (request.tool_call or {}).get("args", {}) or {}
        sql = str(args.get("sql", "") or "")
        verdict, _detail = classify_sql(sql)
        if verdict in ("read", "full_dump"):
            return False
        _logger.info(
            "[sql_approval] 拦截 %s（%s）: %s",
            (request.tool_call or {}).get("name", "?"),
            verdict,
            sql[:120],
        )
        return True

    return when


def _make_description_factory():
    """生成审批卡描述：原因 + 目标库 + SQL 预览。"""

    def describe(tool_call, state, runtime) -> str:  # noqa: ANN001
        args = (tool_call or {}).get("args", {}) or {}
        sql = str(args.get("sql", "") or "")
        verdict, detail = classify_sql(sql)
        if verdict == "write":
            reason = f"写/DDL 操作（{detail}）"
        else:
            reason = "需要人工确认"
        db = str(args.get("db_name", "") or "")
        if not db:
            try:
                from langgraph.config import get_config

                db = str(
                    (get_config().get("configurable", {}) or {}).get("db_name", "")
                    or ""
                )
            except Exception:  # noqa: BLE001
                db = ""
        db_part = f"目标数据库：{db}\n" if db else ""
        return f"⚠️ SQL 执行需要批准 —— {reason}\n{db_part}"

    return describe


def build_sql_approval_middleware(tools):
    """为 run_sql 类工具构建 HumanInTheLoopMiddleware。

    Args:
        tools: 子 agent 解析后的工具列表（按名称匹配 run_sql 变体）。

    Returns:
        中间件实例；无 run_sql 工具时返回 None。
    """
    run_sql_names = [
        getattr(t, "name", "") for t in tools
        if _is_run_sql_tool(getattr(t, "name", "") or "")
    ]
    if not run_sql_names:
        _logger.info("[sql_approval] 未发现 run_sql 工具，跳过审批闸门")
        return None

    from langchain.agents.middleware import HumanInTheLoopMiddleware

    when = _make_when_predicate()
    describe = _make_description_factory()
    interrupt_on = {
        name: {
            # 批准 / 编辑 SQL 后批准 / 拒绝；不提供 respond（避免凭空伪造查询结果）
            "allowed_decisions": ["approve", "edit", "reject"],
            "description": describe,
            "when": when,
        }
        for name in run_sql_names
    }
    _logger.info("[sql_approval] 审批闸门挂载: %s", ", ".join(sorted(run_sql_names)))
    return HumanInTheLoopMiddleware(
        interrupt_on=interrupt_on,
        description_prefix="SQL 执行需要批准",
    )
