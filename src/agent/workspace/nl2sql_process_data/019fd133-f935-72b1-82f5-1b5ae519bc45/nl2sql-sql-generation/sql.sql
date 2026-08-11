SELECT n.primary_name AS 演员姓名,
  COUNT(DISTINCT p.title_id) AS 参演电影数,
  ROUND(AVG(t.average_rating), 2) AS 平均评分
FROM names n
JOIN principals p ON p.name_id = n.id
JOIN titles t ON t.id = p.title_id
WHERE p.category_id IN ('actor', 'actress')
  AND t.title_type = 'movie'
  AND t.num_votes > 50000
GROUP BY n.id, n.primary_name
HAVING COUNT(DISTINCT p.title_id) >= 5
ORDER BY 平均评分 DESC
