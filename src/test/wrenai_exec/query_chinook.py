"""
DuckDB 直接查询 Chinook SQLite 数据库。

用法: python query_chinook.py "SELECT COUNT(*) FROM Artist"
"""

import sys, duckdb

DB = r"D:\code_work_space\llm\nl2sql\src\agent\data\Chinook_Sqlite.sqlite"

sql = sys.argv[1] if len(sys.argv) > 1 else "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"

c = duckdb.connect(DB)
try:
    result = c.sql(sql)
    print(result)
except Exception as e:
    print(f"Error: {e}")
finally:
    c.close()
