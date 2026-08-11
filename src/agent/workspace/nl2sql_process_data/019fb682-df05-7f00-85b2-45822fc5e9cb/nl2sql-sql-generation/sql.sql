SELECT n.primary_name AS actor_name,
  ROUND(AVG(t.average_rating), 2) AS avg_rating,
  COUNT(DISTINCT p.title_id) AS movie_count
FROM names n
JOIN principals p ON p.name_id = n.id
JOIN titles t ON t.id = p.title_id
WHERE p.category_id IN ('actor', 'actress')
  AND t.title_type = 'movie'
  AND t.num_votes > 50000
GROUP BY n.id, n.primary_name
HAVING COUNT(DISTINCT p.title_id) >= 5
ORDER BY AVG(t.average_rating) DESC
