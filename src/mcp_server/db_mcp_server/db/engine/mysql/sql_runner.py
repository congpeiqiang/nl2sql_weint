"""MySQL implementation of SqlRunner interface."""

import logging

# 在类的开头添加日志配置
logger = logging.getLogger(__name__)

import pandas as pd

from mcp_server.db_mcp_server.db.sql_runner import SqlRunner, RunSqlToolArgs, ToolContext
from mcp_server.db_mcp_server.db.limits import mysql_timeout_sqls


def _apply_statement_timeout(conn) -> None:
    """给这条会话设 statement timeout（P2-8）。

    MySQL 5.7.8+ 与 MariaDB 的变量名/单位不同，所以**按序试**（见 ``mysql_timeout_sqls``）；
    全部失败只记 warning、不让查询失败 —— 超时是一层保护，不是查询能成立的前提。
    """
    sqls = mysql_timeout_sqls()
    if not sqls:
        return
    last: Exception | None = None
    for sql in sqls:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            return
        except Exception as e:  # noqa: BLE001  换下一个候选
            last = e
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
    logger.warning("[db-limits] MySQL statement timeout 未设置成功（已试 %s）：%s", sqls, last)


class MySQLRunner(SqlRunner):
    """MySQL implementation of the SqlRunner interface."""

    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 3306,
        **kwargs,
    ):
        """Initialize with MySQL connection parameters.

        Args:
            host: Database host address
            database: Database name
            user: Database user
            password: Database password
            port: Database port (default: 3306)
            **kwargs: Additional PyMySQL connection parameters
        """
        try:
            import pymysql.cursors

            self.pymysql = pymysql
        except ImportError as e:
            raise ImportError(
                "PyMySQL package is required. Install with: pip install 'vanna[mysql]'"
            ) from e

        self.host = host
        self.database = database
        self.user = user
        self.password = password
        self.port = port
        self.kwargs = kwargs

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against MySQL database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results

        Raises:
            pymysql.Error: If query execution fails
        """
        # Connect to the database
        conn = self.pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            port=self.port,
            cursorclass=self.pymysql.cursors.DictCursor,
            **self.kwargs,
        )

        try:
            # Ping to ensure connection is alive
            conn.ping(reconnect=True)

            _apply_statement_timeout(conn)

            cursor = conn.cursor()
            logger.info(f"执行sql: {args.sql}")
            cursor.execute(args.sql)
            results = cursor.fetchall()

            # Create a pandas dataframe from the results
            df = pd.DataFrame(
                results,
                columns=[desc[0] for desc in cursor.description]
                if cursor.description
                else [],
            )

            cursor.close()
            return df

        finally:
            conn.close()
