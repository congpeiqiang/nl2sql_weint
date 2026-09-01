-- 第一步：随机选取一条待删除记录（通过 run_sql 的 limit=1 控制只返回 1 行，SQL 中不写 LIMIT）
SELECT albumid, title, artistid FROM album ORDER BY random();

-- 第二步：删除该条记录（WHERE 使用第一步选中的 albumid 主键，保证只删除一条）
-- DELETE FROM album WHERE albumid = <第一步选中的 albumid>;
