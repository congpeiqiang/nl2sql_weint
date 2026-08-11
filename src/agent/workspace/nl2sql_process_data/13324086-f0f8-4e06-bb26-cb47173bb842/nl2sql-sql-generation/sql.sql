SELECT
  t.start_year AS 年份,
  COUNT(DISTINCT t.id) AS 电影产量,
  ROUND(
    (COUNT(DISTINCT t.id) - LAG(COUNT(DISTINCT t.id)) OVER (ORDER BY t.start_year)) * 100.0
    / NULLIF(LAG(COUNT(DISTINCT t.id)) OVER (ORDER BY t.start_year), 0),
    2
  ) AS 同比增长率
FROM titles t
WHERE t.title_type = 'movie'
  AND t.start_year BETWEEN 2000 AND 2023
  AND t.start_year IS NOT NULL
GROUP BY t.start_year
ORDER BY t.start_year ASC
