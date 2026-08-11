-- 对比导演+编剧双重身份 vs 纯导演的作品平均评分，按类型分组
-- 使用WrenAI语义层模型名（带_t后缀）

WITH director_professions AS (
  -- 找出所有导演及其职业信息
  SELECT DISTINCT
    n.id AS name_id,
    -- 判断该导演是否同时有writer职业
    MAX(CASE WHEN pr.id = 'writer' THEN 1 ELSE 0 END) OVER (PARTITION BY n.id) AS has_writer
  FROM names n
  JOIN names_primaryprofessions npp ON npp.name_id = n.id
  JOIN professions pr ON pr.id = npp.profession_id
  WHERE pr.id = 'director'
),
director_movies AS (
  -- 找出所有导演执导的电影作品
  SELECT DISTINCT
    t.id AS title_id,
    t.average_rating,
    dp.has_writer
  FROM titles t
  JOIN principals p ON p.title_id = t.id AND p.category_id = 'director'
  JOIN names n ON n.id = p.name_id
  JOIN director_professions dp ON dp.name_id = n.id
  WHERE t.title_type = 'movie'
    AND t.num_votes > 1000
    AND t.average_rating IS NOT NULL
)
-- 按类型分组统计
SELECT
  g.display_name AS genre,
  COUNT(DISTINCT CASE WHEN dm.has_writer = 1 THEN dm.title_id END) AS dual_role_count,
  ROUND(AVG(CASE WHEN dm.has_writer = 1 THEN dm.average_rating END), 2) AS dual_role_avg_rating,
  COUNT(DISTINCT CASE WHEN dm.has_writer = 0 THEN dm.title_id END) AS director_only_count,
  ROUND(AVG(CASE WHEN dm.has_writer = 0 THEN dm.average_rating END), 2) AS director_only_avg_rating,
  ROUND(
    AVG(CASE WHEN dm.has_writer = 1 THEN dm.average_rating END) -
    AVG(CASE WHEN dm.has_writer = 0 THEN dm.average_rating END),
    2
  ) AS rating_difference
FROM director_movies dm
JOIN titles_genres tg ON tg.title_id = dm.title_id
JOIN genres g ON g.id = tg.genre_id
GROUP BY g.display_name
HAVING COUNT(DISTINCT CASE WHEN dm.has_writer = 1 THEN dm.title_id END) > 0
   AND COUNT(DISTINCT CASE WHEN dm.has_writer = 0 THEN dm.title_id END) > 0
ORDER BY rating_difference DESC
