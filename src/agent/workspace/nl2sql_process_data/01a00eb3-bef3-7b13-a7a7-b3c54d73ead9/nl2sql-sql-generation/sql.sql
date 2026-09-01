-- ============================================================
-- 任务：随机删除 playlisttrack 表的一条记录（Chinook_Aliyun / PostgreSQL）
-- 通道：dbmcp 直连（无 dry_run 工具，用同构 SELECT 语法预验证替代）
-- ============================================================

-- [1] 随机选取一条待删除记录（不写 LIMIT，通过 run_sql limit=1 控制）
SELECT playlistid, trackid FROM playlisttrack ORDER BY random();

-- [2] 删除该记录（playlistid/trackid 在执行阶段回填随机结果）
-- DELETE FROM playlisttrack WHERE playlistid = <X> AND trackid = <Y>;

-- [3] 删除确认（验证该主键已不存在）
-- SELECT playlistid, trackid FROM playlisttrack WHERE playlistid = <X> AND trackid = <Y>;
