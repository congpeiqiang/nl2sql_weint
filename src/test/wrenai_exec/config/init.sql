-- DuckDB init: attach Chinook SQLite to wren.public schema
ATTACH 'D:/code_work_space/llm/nl2sql/src/agent/data/Chinook_Sqlite.sqlite' AS chinook_src (TYPE SQLITE);
CREATE SCHEMA IF NOT EXISTS wren;
CREATE SCHEMA IF NOT EXISTS public;
