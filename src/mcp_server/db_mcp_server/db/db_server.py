"""FastMCP server exposing NL2SQL SqlRunner tools.

Supports three transports:
- ``stdio`` (default): Standard MCP stdin/stdout transport
- ``sse``: Server-Sent Events over HTTP (legacy)
- ``http``: Streamable HTTP transport (recommended for web)

Run via CLI::

    NL2SQL-mcp --transport http --host 0.0.0.0 --port 8080

Or via environment variables::

    export NL2SQL_MCP_TRANSPORT=http
    export NL2SQL_MCP_HOST=0.0.0.0
    export NL2SQL_MCP_PORT=8080
    python -m NL2SQL.servers.mcp.server
"""

import logging
import re
from typing import Any, Dict, Optional

import click
import pandas as pd
from fastmcp import FastMCP

from mcp_server.db_mcp_server.db.core.settings import settings
from mcp_server.db_mcp_server.db.sql_runner import RunSqlToolArgs, ToolContext

from mcp_server.db_mcp_server.db.config import McpSqlConfig
from mcp_server.db_mcp_server.db.limits import effective_row_cap
from mcp_server.db_mcp_server.db.multi_sql import split_sql_statements, combine_multi_results, df_to_result as _h_df_to_result

_logger = logging.getLogger(__name__)

# 结尾的「裸整数 LIMIT」（可带分号/空白）。**故意不匹配 `LIMIT n OFFSET m`** ——
# 那种形态没法就地改写（改错会变语法错误），留给下一档的「无 LIMIT 才追加」之外的兜底。
_TRAILING_INT_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\s*;?\s*$", re.IGNORECASE)
# 词边界判 LIMIT 是否存在：旧的 `"LIMIT" not in sql.upper()` 会把 `LIMITED` 之类的
# 子串也算命中，从而**该注入时不注入**。
_HAS_LIMIT_RE = re.compile(r"\bLIMIT\b", re.IGNORECASE)


def _apply_default_limit(stmt: str, limit: int, *, clamp_existing: bool = False) -> str:
    """给 SELECT/WITH 语句收敛/追加 LIMIT（服务端行数上限）。

    与语义层 run_sql 契约一致：SQL 已含 LIMIT 时不动；非 SELECT 语句不动。

    改造前只有「全文不含 LIMIT 就追加」一条分支，于是 `LIMIT 999999999` 这种
    **模型自己写的大 LIMIT 能原样下发**，结果全量进内存。现在三档：

    1. 结尾是裸整数 LIMIT → `clamp_existing=True` 时**收敛到上限**；否则原样
    2. 全文无 LIMIT 且是 SELECT/WITH → 追加
    3. 其它（含 `LIMIT n OFFSET m`、注释/字符串里出现过 LIMIT）→ 原样不动

    ⚠️ 第 3 档是**已知的放行**，不能指望它；真正的兜底是 ``run_sql`` 取数后的按行硬截断
    （文本判据可以被绕过，行数绕不过去）。

    ``clamp_existing`` 为什么默认关：**agent 直连通道**要收敛（上限就是该通道的硬闸），
    但 ``api/feedback_annotation._run_preview`` 传进来的可能是**语义层已按连接器上限
    处理过的物理 SQL**（见该文件 :634 的注释），把它按 `_PREVIEW_LIMIT` 压小会让
    「口径试算的行数与线上工具不一致」——那是那个功能的立身之本。口径不同就得显式分开。
    """
    cap = effective_row_cap(limit)
    s = stmt.strip()
    if not s:
        return s

    m = _TRAILING_INT_LIMIT_RE.search(s)
    if m:
        given = int(m.group(1))
        if clamp_existing and given > cap:
            # 只换掉那个数字（`m.start(1)`），保留原来的 `LIMIT`/`limit` 写法与前后空白
            clamped = s[: m.start(1)] + str(cap)
            _logger.warning(
                "[db-limits] SQL 自带 LIMIT %d 超过上限 %d，已收敛为 %d", given, cap, cap
            )
            return clamped
        return s

    if s.upper().startswith(("SELECT", "WITH")) and not _HAS_LIMIT_RE.search(s):
        return f"{s} LIMIT {cap}"
    return s


# 各 db_type 获取表清单的 SQL（postgres 需要 information_schema 查询）。
_TABLE_LIST_SQL: Dict[str, str] = {
    "mysql": "SHOW TABLES",
    "clickhouse": "SHOW TABLES",
    "sqlite": "SELECT name AS name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
    "postgres": "SELECT tablename AS name FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY tablename",
}

# Mapping of database type names to their runner classes.
_RUNNER_REGISTRY: Dict[str, str] = {
    "mysql": "mcp_server.db_mcp_server.db.engine.mysql.sql_runner.MySQLRunner",
    "clickhouse": "mcp_server.db_mcp_server.db.engine.clickhouse.sql_runner.ClickHouseRunner",
    "postgres": "mcp_server.db_mcp_server.db.engine.postgres.sql_runner.PostgresRunner",
    "sqlite": "mcp_server.db_mcp_server.db.engine.sqlite.sql_runner.SqliteRunner",
}


def _load_runner_class(db_type: str):
    """Dynamically import the SqlRuner class for the given database type."""
    import importlib

    class_path = _RUNNER_REGISTRY.get(db_type)
    if class_path is None:
        supported = ", ".join(_RUNNER_REGISTRY.keys())
        raise ValueError(
            f"Unsupported db_type: {db_type!r}. Supported: {supported}"
        )

    module_path, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _build_tool_context() -> ToolContext:
    """Build a minimal ToolContext for runner invocation."""
    return ToolContext.model_construct(
        user="user_001",
        metadata={},
    )


class NL2SQLMcpSqlServer:
    """FastMCP server that exposes a unified run_sql tool backed by NL2SQL runners."""

    def __init__(self, config: Optional[McpSqlConfig] = None):
        self._config = config or McpSqlConfig.from_env()
        self._runner_cache: dict = {}  # db_name → runner
        self._context = _build_tool_context()
        self.mcp = FastMCP("NL2SQL SQL")
        self._register_tools()

    def _get_runner(self, db_name: str):
        """获取指定数据库的 runner，db_name 必传"""
        if not db_name:
            raise ValueError("db_name 不能为空——请从前端选择数据库")
        if db_name not in self._runner_cache:
            cfg = McpSqlConfig.from_env(db_name)
            runner_cls = _load_runner_class(cfg.db_type)
            self._runner_cache[db_name] = runner_cls(**cfg.config)
        return self._runner_cache[db_name]

    def _register_tools(self) -> None:
        @self.mcp.tool()
        async def run_sql(sql: str, db_name: str = "", limit: int = 1000) -> Dict[str, Any]:
            """【直连通道】直接连接指定的数据库执行 SQL。

            与语义层工具（wrenai_run_sql）不同，本工具不经过语义层，
            直接连接 db_name 指向的数据库执行 SQL——适用于未在语义层建模的库，
            或需要直接 SQL 访问（DDL/DML/多语句）的场景。

            支持场景：
            - 单条 SELECT / INSERT / UPDATE / DELETE
            - 多条语句：`CREATE TABLE ...; INSERT INTO ...; SELECT ...;`
            - DDL + DML 混合：先建表、再插入数据、最后查询

            Args:
                sql: SQL 语句，多条语句以分号 `;` 分隔
                db_name: 数据库名（前端「数据库」下拉框选中的配置名，必须已配置）
                limit: SELECT 语句的返回行数上限（与语义层 run_sql 契约一致）。
                    服务端会把 SQL 里自带的裸整数 LIMIT **收敛**到该值、给没有 LIMIT 的
                    SELECT/WITH 追加该值，并在取数后按行数硬截断；上限本身也被
                    ``NL2SQL_DB_MAX_ROW_LIMIT``（默认 10000）封顶。传 0/负数按默认 1000。

            Returns:
                包含 columns、rows、row_count 的字典。
                发生过截断时额外包含 ``truncated`` 与 ``truncated_note``。
                多语句时额外包含 statement_count 和 statements 执行摘要。
            """
            statements = split_sql_statements(sql)
            runner = self._get_runner(db_name)
            cap = effective_row_cap(limit)

            all_results: list = []
            truncated: list[dict] = []
            for stmt in statements:
                # clamp_existing=True：本条通道的 cap 就是硬闸 —— 模型自己写的
                # `LIMIT 999999999` 也要收敛（这是唯一能堵住它的地方）。
                stmt = _apply_default_limit(stmt, cap, clamp_existing=True)
                args = RunSqlToolArgs(sql=stmt)
                df = await runner.run_sql(args, self._context)
                # 取数后的硬截断：注入那一层靠文本判断，绕得过去（子查询 LIMIT、
                # `LIMIT n OFFSET m`、注释里出现 LIMIT）；这一层只看事实行数。
                if len(df) > cap:
                    _logger.warning(
                        "[db-limits] 单条语句返回 %d 行，超过上限 %d，已截断（db=%s）",
                        len(df), cap, db_name,
                    )
                    truncated.append({"rows": int(len(df)), "kept": cap})
                    df = df.iloc[:cap]
                all_results.append((stmt, df))

            result = combine_multi_results(all_results)
            if truncated:
                result["truncated"] = True
                result["truncated_note"] = (
                    f"结果被截断到 {cap} 行（原始行数："
                    f"{', '.join(str(t['rows']) for t in truncated)}）。"
                    "如需完整数据请加筛选条件缩小范围，或改用聚合查询。"
                )
            return result

        @self.mcp.tool()
        async def get_db_info(db_name: str = "") -> Dict[str, Any]:
            """返回指定数据库的连接信息与表清单（子 agent 建查询用）。

            Args:
                db_name: 前端选中的数据库名（db_config 中的 name）

            Returns:
                db_name、db_type 与 tables（表名列表）。表清单查询失败时
                附 tables_error 字段，不抛异常。
            """
            runner = self._get_runner(db_name)
            cfg = McpSqlConfig.from_env(db_name)
            info: Dict[str, Any] = {
                "db_name": db_name,
                "db_type": cfg.db_type,
                "tables": [],
            }
            try:
                list_sql = _TABLE_LIST_SQL.get(cfg.db_type, "SHOW TABLES")
                # 这条也不经 `_apply_default_limit`（改造前就没经），但同样按行数封顶：
                # 表清单天生有界，这里只是不让「唯一一个绕过注入的入口」成为例外。
                df = await runner.run_sql(RunSqlToolArgs(sql=list_sql), self._context)
                info["tables"] = [str(v) for v in df.iloc[:, 0].tolist()][: effective_row_cap(0)]
            except Exception as e:  # noqa: BLE001
                info["tables_error"] = f"{type(e).__name__}: {e}"
            return info

    @property
    def http_app(self):
        """Return the ASGI/HTTP app for external servers (uvicorn, gunicorn, etc.).

        Example::

            uvicorn NL2SQL.servers.mcp.server:http_app --host 0.0.0.0 --port 8000
        """
        return self.mcp.http_app(path="/mcp")

    def run(
        self,
        transport: str = "stdio",
        host: str = "0.0.0.0",
        port: int = 8000,
    ) -> None:
        """Start the FastMCP server.

        Args:
            transport: One of ``stdio``, ``sse``, or ``http``.
            host: Bind address for SSE/HTTP transports.
            port: Bind port for SSE/HTTP transports.
        """
        if transport == "stdio":
            self.mcp.run()
        elif transport in ("sse", "http"):
            self.mcp.run(transport=transport, host=host, port=port)
        else:
            raise ValueError(
                f"Unsupported transport: {transport!r}. "
                "Choose from: stdio, sse, http"
            )


@click.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse", "http"], case_sensitive=False),
    default=lambda: settings.NL2SQL_MCP_TRANSPORT,
    help="MCP transport protocol",
)
@click.option(
    "--host",
    default=lambda: settings.NL2SQL_MCP_HOST,
    help="Bind host for SSE/HTTP transports",
)
@click.option(
    "--port",
    type=int,
    default=lambda: settings.NL2SQL_MCP_PORT,
    help="Bind port for SSE/HTTP transports",
)
def main(transport: str, host: str, port: int) -> None:
    """Run the NL2SQL MCP SQL server."""
    server = NL2SQLMcpSqlServer()

    if transport == "stdio":
        # stdio 模式下 stdout 是 JSONRPC 通道，横幅必须走 stderr，否则会破坏协议帧
        click.echo("[NL2SQL-dbmcp] starting SQL server (stdio)", err=True)
        server.run(transport="stdio")
    elif transport == "sse":
        click.echo(
            f"[NL2SQL-dbmcp] starting SQL server (SSE) on http://{host}:{port}/sse",
            err=True,
        )
        server.run(transport="sse", host=host, port=port)
    else:
        click.echo(
            f"[NL2SQL-dbmcp] starting SQL server (HTTP) on http://{host}:{port}/mcp",
            err=True,
        )
        server.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
