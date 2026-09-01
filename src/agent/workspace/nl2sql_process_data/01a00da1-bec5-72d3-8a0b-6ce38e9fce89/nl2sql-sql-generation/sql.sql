-- 步骤1：确认目标记录存在情况（验证查询）
SELECT id, primary_title, title_type, start_year
FROM titles
WHERE primary_title = 'The Ballad of Conrad & Vernon';

-- 步骤2：删除目标记录
DELETE FROM titles
WHERE primary_title = 'The Ballad of Conrad & Vernon';
