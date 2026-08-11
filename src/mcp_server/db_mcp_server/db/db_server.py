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


# Mapping of database type names to their runner classes.
_RUNNER_REGISTRY: Dict[str, str] = {
    "mysql": "mcp_server.db_mcp_server.db.engine.mysql.sql_runner.MySQLRunner",
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
        async def run_sql(sql: str, db_name: str = "") -> Dict[str, Any]:
            """执行一条或多条 SQL 语句（以分号 `;` 分隔）。

            支持场景：
            - 单条 SELECT / INSERT / UPDATE / DELETE
            - 多条语句：`CREATE TABLE ...; INSERT INTO ...; SELECT ...;`
            - DDL + DML 混合：先建表、再插入数据、最后查询

            Args:
                sql: SQL 语句，多条语句以分号 `;` 分隔

            Returns:
                包含 columns、rows、row_count 的字典。
                多语句时额外包含 statement_count 和 statements 执行摘要。
            """
            statements = split_sql_statements(sql)
            runner = self._get_runner(db_name)

            all_results: list = []
            for stmt in statements:
                args = RunSqlToolArgs(sql=stmt)
                df = await runner.run_sql(args, self._context)
                all_results.append((stmt, df))

            return combine_multi_results(all_results)

        @self.mcp.tool()
        def get_db_info() -> Dict[str, Any]:
            """Return information about the currently configured database."""
            return {
                "db_type": self._config.db_type,
                "config_keys": list(self._config.config.keys()),
            }

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
        click.echo("🚀 Starting NL2SQL MCP SQL server (stdio)")
        server.run(transport="stdio")
    elif transport == "sse":
        click.echo(
            f"🚀 Starting NL2SQL MCP SQL server (SSE) on http://{host}:{port}/sse"
        )
        server.run(transport="sse", host=host, port=port)
    else:
        click.echo(
            f"🚀 Starting NL2SQL MCP SQL server (HTTP) on http://{host}:{port}/mcp"
        )
        server.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
