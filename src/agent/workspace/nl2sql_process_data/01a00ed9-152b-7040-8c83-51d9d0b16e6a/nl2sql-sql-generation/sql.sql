-- Step 5.1 确认 SQL：查询 albumid 最大的一条记录（limit 由 run_sql 参数控制，SQL 中不写 LIMIT）
SELECT albumid, title, artistid FROM album ORDER BY albumid DESC;

-- Step 5.2 删除 SQL：删除 albumid 最大的一条记录（MAX_ID 以 5.1 确认结果为准）
-- DELETE FROM album WHERE albumid = <MAX_ID>;
