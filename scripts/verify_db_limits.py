# -*- coding: utf-8 -*-
"""P2-8 目标库访问两道闸（statement timeout + 结果行数上限）验证。

跑法：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_db_limits.py

⚠️ **安全前提**：本脚本**不连任何真实业务库**。e2e 那一段用临时目录里的 sqlite 文件，
   并且只走 `SqliteRunner`（无外部依赖）。statement timeout 只验证「生成的语句对不对」，
   真正下发到 postgres/mysql/clickhouse 的验证需要那些库在场，不在本脚本能力范围内
   （已在报告里写明）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

# 先清干净，保证测的是**默认值**而不是本机/CI 残留的 env
for _k in (
    "NL2SQL_DB_STATEMENT_TIMEOUT_SECS",
    "NL2SQL_DB_MAX_ROW_LIMIT",
):
    os.environ.pop(_k, None)

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    results.append((bool(cond), label))
    tag = "PASS" if cond else "FAIL"
    color = "\033[32m" if cond else "\033[31m"
    line = f"  [{color}{tag}\033[0m] {label}"
    if detail:
        line += f"  {detail}"
    print(line, flush=True)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def _reload_limits(**env):
    """按需改 env 后重新导入 limits 模块（读 env 的函数是运行期读的，不用重导入——
    但 `_env_float` 走 os.getenv，所以只要 setenv 即可）。"""
    import importlib

    from mcp_server.db_mcp_server.db import limits as L

    importlib.reload(L)
    return L


# ── ① 行数上限：入参不可信，必须收敛 ──────────────────────────────
def verify_row_cap() -> None:
    section("① 行数上限：模型给的 limit 当成不可信输入")
    L = _reload_limits()
    check(L.effective_row_cap(None) == 1000, "limit=None → 默认 1000")
    check(L.effective_row_cap(0) == 1000, "limit=0 → 默认 1000（不是「不限」）")
    check(L.effective_row_cap(-5) == 1000, "limit=-5 → 默认 1000")
    check(L.effective_row_cap("abc") == 1000, "limit='abc'（非数字）→ 默认 1000，不抛异常")
    check(L.effective_row_cap(500) == 500, "limit=500 → 500（小于上限就尊重）")
    check(L.effective_row_cap(10**9) == 10000, "limit=10**9 → 硬上限 10000（越不过去）")
    check(L.effective_row_cap(10000) == 10000, "limit=10000 → 恰好等于硬上限，保留")
    check(L.effective_row_cap(10001) == 10000, "limit=10001 → 收敛到 10000")

    os.environ["NL2SQL_DB_MAX_ROW_LIMIT"] = "abc"
    L = _reload_limits()
    check(L.effective_row_cap(10**9) == 10000, "硬上限 env 非数字 → 退回 10000（不静默关掉保护）")
    os.environ["NL2SQL_DB_MAX_ROW_LIMIT"] = "200"
    L = _reload_limits()
    check(L.effective_row_cap(10**9) == 200, "硬上限 env=200 生效（运维可下调，不必改码）")
    check(L.effective_row_cap(50) == 50, "负对照：低于硬上限的值不受影响")
    os.environ.pop("NL2SQL_DB_MAX_ROW_LIMIT")


# ── ② statement timeout：默认值、0=关、非数字退回默认 ──────────────
def verify_statement_timeout_config() -> None:
    section("② statement timeout：默认 240s（必须 < 工具超时 300s）、0=关、非数字退回默认")
    L = _reload_limits()
    check(L.statement_timeout_secs() == 240.0, "默认 240s")
    check(L.statement_timeout_secs() < 300, "默认值严格小于工具超时 300s（让数据库先中止）")
    check(L.statement_timeout_sql("postgres") == "SET statement_timeout = 240000",
          "postgres 生成 SET statement_timeout（毫秒）", str(L.statement_timeout_sql("postgres")))
    check(L.statement_timeout_sql("sqlite") is None, "sqlite 刻意不做（无该机制）")
    check(len(L.mysql_timeout_sqls()) == 2, "mysql 给两个候选（MySQL / MariaDB 变量名不同）")
    check("MAX_EXECUTION_TIME = 240000" in L.mysql_timeout_sqls()[0],
          "mysql 候选①是 MySQL 的毫秒变量", L.mysql_timeout_sqls()[0])
    check("max_statement_time = 240" in L.mysql_timeout_sqls()[1],
          "mysql 候选②是 MariaDB 的秒变量", L.mysql_timeout_sqls()[1])
    check(L.clickhouse_settings() == {"max_execution_time": 240.0},
          "clickhouse 走 settings（秒）", str(L.clickhouse_settings()))

    os.environ["NL2SQL_DB_STATEMENT_TIMEOUT_SECS"] = "0"
    L = _reload_limits()
    check(L.statement_timeout_secs() == 0.0 and L.statement_timeout_sql("postgres") is None
          and L.mysql_timeout_sqls() == [] and L.clickhouse_settings() == {},
          "0 = 关闭（四条路径同时失效，不留半开）")
    os.environ["NL2SQL_DB_STATEMENT_TIMEOUT_SECS"] = "-30"
    L = _reload_limits()
    check(L.statement_timeout_secs() == 0.0, "负值按关闭算")
    os.environ["NL2SQL_DB_STATEMENT_TIMEOUT_SECS"] = "abc"
    L = _reload_limits()
    check(L.statement_timeout_secs() == 240.0, "非数字 → 退回默认（不是静默关闭）")
    os.environ.pop("NL2SQL_DB_STATEMENT_TIMEOUT_SECS")


# ── ③ SQL 改写：三档 + 旧嗅探的回归 ──────────────────────────────
def verify_apply_limit() -> None:
    section("③ SQL 改写：收敛自带大 LIMIT / 无 LIMIT 才追加 / 其余不动")
    _reload_limits()
    from mcp_server.db_mcp_server.db.db_server import _apply_default_limit as f

    def clamp(sql: str, limit: int) -> str:
        """agent 直连通道的用法（要收敛自带 LIMIT）。"""
        return f(sql, limit, clamp_existing=True)

    check(f("SELECT * FROM t", 1000) == "SELECT * FROM t LIMIT 1000",
          "无 LIMIT 的 SELECT → 追加")
    check(f("WITH x AS (SELECT 1) SELECT * FROM x", 1000).endswith("LIMIT 1000"),
          "WITH ... SELECT 也追加（与语义层契约一致）")
    check(clamp("SELECT * FROM t LIMIT 999999999", 1000) == "SELECT * FROM t LIMIT 1000",
          "**自带 LIMIT 过大 → 收敛**（改造前会原样下发，全量进内存）")
    check(clamp("select * from t limit 999999999", 1000) == "select * from t limit 1000",
          "小写 limit 同样收敛，且**保留原大小写**（只换数字）")
    check(clamp("SELECT * FROM t LIMIT 5", 1000) == "SELECT * FROM t LIMIT 5",
          "负对照：自带 LIMIT 小于上限 → 不动")
    check(clamp("SELECT * FROM t LIMIT 1000;", 1000) == "SELECT * FROM t LIMIT 1000;",
          "带分号的 LIMIT → 不动（也不多吃一个分号）")
    check(f("INSERT INTO t VALUES (1)", 1000) == "INSERT INTO t VALUES (1)",
          "非 SELECT 不追加（写语句不能被塞 LIMIT）")
    check(f("SELECT * FROM t LIMIT 5 OFFSET 3", 1000) == "SELECT * FROM t LIMIT 5 OFFSET 3",
          "已知放行：LIMIT .. OFFSET .. 原样不动（改错会变语法错误）")
    check(f("SELECT * FROM t WHERE c = 'LIMIT'", 1000) == "SELECT * FROM t WHERE c = 'LIMIT'",
          "已知放行：字符串里出现 LIMIT → 不动（不冒险改文本）")
    check(f("SELECT * FROM v_limited", 1000) == "SELECT * FROM v_limited LIMIT 1000",
          "回归：`v_limited` 不再被旧的全文字串嗅探误判（旧写法会跳过注入）")
    check(clamp("SELECT * FROM t LIMIT 999999999", 10**9) == "SELECT * FROM t LIMIT 10000",
          "limit 入参本身也是不可信的：10**9 被收敛到硬上限 10000")
    check(clamp("SELECT * FROM t LIMIT 999999999", 0) == "SELECT * FROM t LIMIT 1000",
          "limit=0 按默认 1000 收敛（不是「不限」）")
    check(f("   ", 1000) == "", "空白语句不炸")

    # 负对照：默认**不**收敛自带 LIMIT。`api/feedback_annotation._run_preview` 传进来的
    # 可能是语义层已按连接器上限处理过的物理 SQL，压小它会让「口径试算与线上工具行数
    # 不一致」——那个功能的立身之本。所以默认必须保持改造前的跳过行为。
    check(f("SELECT * FROM t LIMIT 10000", 100) == "SELECT * FROM t LIMIT 10000",
          "负对照：默认 clamp_existing=False ⇒ 自带 LIMIT 原样（护住 cube 口径试算契约）")
    check(f("SELECT * FROM t LIMIT 5 OFFSET 3", 100, clamp_existing=True)
          == "SELECT * FROM t LIMIT 5 OFFSET 3",
          "负对照：开了收敛也不会去改 OFFSET 形态（两种口径同一条放行规则）")


# ── ④ e2e：真 sqlite 库，走 db_server 的 MCP 工具入口 ──────────────
def verify_e2e_sqlite() -> None:
    section("④ e2e：临时 sqlite 库 → FastMCP run_sql 工具（真取数、真截断）")
    import asyncio

    from fastmcp import FastMCP
    from mcp_server.db_mcp_server.db import db_server as S

    work = Path(tempfile.mkdtemp(prefix="nl2sql-verify-p28-"))
    db_path = work / "t.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE big (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO big VALUES (?, ?)",
                     [(i, f"n{i}") for i in range(5000)])
    conn.commit()
    conn.close()

    from mcp_server.db_mcp_server.db.engine.sqlite.sql_runner import SqliteRunner

    class _Stub:  # 只换掉「连哪个库」，其余走真实现
        def __init__(self, runner):
            self._r = runner

        async def run_sql(self, args, context):
            return await self._r.run_sql(args, context)

    runner = SqliteRunner(database_path=str(db_path))
    srv = S.NL2SQLMcpSqlServer.__new__(S.NL2SQLMcpSqlServer)
    srv._config = None
    srv._runner_cache = {"stub": _Stub(runner)}
    srv._context = S._build_tool_context()
    srv.mcp = FastMCP("verify-db-limits")
    srv._register_tools()

    def _payload(got):
        """FastMCP 返回 ToolResult；结构化结果在 `structured_content`。"""
        sc = getattr(got, "structured_content", None)
        if isinstance(sc, dict):
            return sc
        if isinstance(got, tuple) and len(got) == 2:
            return got[1]
        return got

    async def call(sql: str, limit: int = 1000):
        got = await srv.mcp.call_tool("run_sql", {"sql": sql, "db_name": "stub", "limit": limit})
        return _payload(got)

    d = asyncio.run(call("SELECT * FROM big", 1000))
    check(d["row_count"] == 1000, "SELECT 全表 5000 行 → 只回 1000 行（追加 LIMIT 生效）",
          f"row_count={d['row_count']}")
    check(not d.get("truncated"),
          "追加 LIMIT 就够的形态**不**标截断（截断标记只在真发生时才出现，不假报）",
          str(d.get("truncated")))

    d = asyncio.run(call("SELECT * FROM big LIMIT 999999999", 1000))
    check(d["row_count"] == 1000 and not d.get("truncated"),
          "自带 LIMIT 999999999 → 被收敛后数据库只回 1000 行（不靠截断兜底）",
          f"row_count={d['row_count']} truncated={d.get('truncated')}")

    d = asyncio.run(call("SELECT * FROM big LIMIT 7", 1000))
    check(d["row_count"] == 7 and not d.get("truncated"),
          "负对照：LIMIT 7 → 回 7 行且不标截断", f"row_count={d['row_count']}")

    d = asyncio.run(call("SELECT * FROM big LIMIT 5 OFFSET 100", 1000))
    check(d["row_count"] == 5, "已知放行档：LIMIT 5 OFFSET 100 正常返回 5 行",
          f"row_count={d['row_count']}")

    # **取数后硬截断**这一层：`LIMIT n OFFSET m` 是「已知放行」档（改写不安全、注入跳过），
    # 数据库真回 4000 行 ⇒ 必须靠行数硬截断才拦得住。这是本项最关键的负对照。
    d = asyncio.run(call("SELECT * FROM big LIMIT 4000 OFFSET 0", 1000))
    check(d["row_count"] == 1000 and bool(d.get("truncated")),
          "**文本改写绕得过去的形态，靠取数后的行数硬截断拦住**",
          f"row_count={d['row_count']} truncated={d.get('truncated')}")
    check("4000" in str(d.get("truncated_note")),
          "截断提示里带上真实原始行数（agent 才知道被砍了）",
          str(d.get("truncated_note"))[:90])

    # CTE：改造前 postgres/sqlite runner 按首词判 SELECT，`WITH` 会被判成非查询、结果丢掉
    d = asyncio.run(call("WITH x AS (SELECT id FROM big) SELECT COUNT(*) AS c FROM x", 1000))
    check(d["row_count"] == 1 and d["rows"][0]["c"] == 5000,
          "CTE（WITH ... SELECT）能取回结果（改造前 sqlite 按首词判类型会把结果丢掉）",
          str(d["rows"][:1]))

    # limit 入参本身越界：被收敛到硬上限 10000，表只有 5000 行 ⇒ 全量返回
    d = asyncio.run(call("SELECT * FROM big", 10**9))
    check(d["row_count"] == 5000,
          "limit=10**9 → 收敛到硬上限 10000；表只有 5000 行 ⇒ 全量（没有被无限放大）",
          f"row_count={d['row_count']}")

    # get_db_info 那条绕开注入的入口（它要读 db 配置，打个桩只换配置来源）
    class _Cfg:
        db_type = "sqlite"

    _orig = S.McpSqlConfig.from_env
    S.McpSqlConfig.from_env = classmethod(lambda cls, name=None: _Cfg())  # type: ignore[assignment]
    try:
        idata = _payload(asyncio.run(srv.mcp.call_tool("get_db_info", {"db_name": "stub"})))
    finally:
        S.McpSqlConfig.from_env = _orig  # type: ignore[assignment]
    check(idata.get("tables") == ["big"], "get_db_info 仍能列表（且同样按行数封顶）",
          str(idata.get("tables")))

    print(f"  （临时库：{db_path}）")


def main() -> int:
    verify_row_cap()
    verify_statement_timeout_config()
    verify_apply_limit()
    verify_e2e_sqlite()
    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"{passed}/{total} 通过")
    if passed != total:
        print("失败项：")
        for ok, label in results:
            if not ok:
                print(f"  ✗ {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
