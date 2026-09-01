-- 查询 Chinook_Aliyun (PostgreSQL/chinook) 中全部业务表的名称及总数
-- 使用窗口函数 COUNT(*) OVER () 在每一行附带总表数，一条 SQL 同时给出"数量"与"表名清单"
-- 不写 LIMIT：由 run_sql 的 limit 参数控制行数（工具自动追加 cap，避免语法冲突）
SELECT
    table_name,
    COUNT(*) OVER () AS total_tables
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name ASC;
