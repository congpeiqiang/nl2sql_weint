"""Oracle implementation of SqlRunner interface."""
# noqa  MC8zOmFIVnBZMlhrdUp2bG43bmx2TG82Y1ZkWWFnPT06NzZhOWM1OWI=

import pandas as pd

from db.sql_runner import SqlRunner, RunSqlToolArgs, ToolContext


class OracleRunner(SqlRunner):
    """Oracle implementation of the SqlRunner interface."""

    def __init__(self, user: str, password: str, dsn: str, **kwargs):
        """Initialize with Oracle connection parameters.

        Args:
            user: Oracle database user name
            password: Oracle database user password
            dsn: Oracle database host - format: host:port/sid
            **kwargs: Additional oracledb connection parameters
        """
        try:
            import oracledb

            self.oracledb = oracledb
        except ImportError as e:
            raise ImportError(
                "oracledb package is required. Install with: pip install 'vanna[oracle]'"
            ) from e

        self.user = user
        self.password = password
        self.dsn = dsn
        self.kwargs = kwargs

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against Oracle database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results

        Raises:
            oracledb.Error: If query execution fails
        """
        # Connect to the database
        conn = self.oracledb.connect(
            user=self.user, password=self.password, dsn=self.dsn, **self.kwargs
        )
# pylint: disable  MS8zOmFIVnBZMlhrdUp2bG43bmx2TG82Y1ZkWWFnPT06NzZhOWM1OWI=

        cursor = conn.cursor()

        try:
            # Strip and remove trailing semicolons (Oracle doesn't like them)
            sql = args.sql.rstrip()
            if sql.endswith(";"):
                sql = sql[:-1]

            # Execute the query
            cursor.execute(sql)
            results = cursor.fetchall()
# pragma: no cover  Mi8zOmFIVnBZMlhrdUp2bG43bmx2TG82Y1ZkWWFnPT06NzZhOWM1OWI=

            # Create a pandas dataframe from the results
            df = pd.DataFrame(results, columns=[desc[0] for desc in cursor.description])
            return df

        except self.oracledb.Error:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
