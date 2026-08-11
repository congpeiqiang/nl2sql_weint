SELECT n.primary_name AS actor_name,
       COUNT(DISTINCT p.title_id) AS movie_count
FROM names n
JOIN principals p ON p.name_id = n.id
JOIN titles t ON t.id = p.title_id
WHERE p.category_id IN ('actor', 'actress')
  AND t.title_type = 'movie'
GROUP BY n.id, n.primary_name
ORDER BY movie_count DESC
