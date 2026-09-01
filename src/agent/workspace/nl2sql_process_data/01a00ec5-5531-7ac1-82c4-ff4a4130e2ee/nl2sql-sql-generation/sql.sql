-- 任务：从 album 表随机删除一条记录（不可逆，只删一条）
-- 已通过 SELECT ... ORDER BY random() 确认待删除记录：albumid = 316
-- 主键 albumid 唯一确定一行，保证只删除一条；无外键引用 album，不会违反约束
DELETE FROM album WHERE albumid = 316;
