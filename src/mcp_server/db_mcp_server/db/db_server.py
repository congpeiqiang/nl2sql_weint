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

from typing import Any, Dict, Optional

import click
import pandas as pd
from fastmcp import FastMCP

from mcp_server.db_mcp_server.db.core.settings import settings
from mcp_server.db_mcp_server.db.sql_runner import RunSqlToolArgs, ToolContext

from mcp_server.db_mcp_server.db.config import McpSqlConfig
from mcp_server.db_mcp_server.db.multi_sql import split_sql_statements, combine_multi_results, df_to_result as _h_df_to_result


def _apply_default_limit(stmt: str, limit: int) -> str:
    """给 SELECT/WITH 语句追加 LIMIT（服务端默认行数上限）。

    与语义层 run_sql 契约一致：SQL 已含 LIMIT 时跳过；非 SELECT 语句不动。
    """
    if not limit or limit <= 0:
        return stmt
    s = stmt.strip()
    if not s:
        return s
    upper = s.upper()
    if upper.startswith(("SELECT", "WITH")) and "LIMIT" not in upper:
        return f"{s} LIMIT {int(limit)}"
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
                limit: SELECT 语句的默认返回行数上限（与语义层 run_sql 契约一致，
                    服务端自动给 SELECT 追加 LIMIT；SQL 已含 LIMIT 时不重复追加）。

            Returns:
                包含 columns、rows、row_count 的字典。
                多语句时额外包含 statement_count 和 statements 执行摘要。
            """
            statements = split_sql_statements(sql)
            runner = self._get_runner(db_name)

            all_results: list = []
            for stmt in statements:
                stmt = _apply_default_limit(stmt, limit)
                args = RunSqlToolArgs(sql=stmt)
                df = await runner.run_sql(args, self._context)
                all_results.append((stmt, df))

            return combine_multi_results(all_results)

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
                df = await runner.run_sql(RunSqlToolArgs(sql=list_sql), self._context)
                info["tables"] = [str(v) for v in df.iloc[:, 0].tolist()]
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
