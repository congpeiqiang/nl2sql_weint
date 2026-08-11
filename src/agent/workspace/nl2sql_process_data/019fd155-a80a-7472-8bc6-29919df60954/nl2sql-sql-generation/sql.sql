SELECT n.primary_name AS actor_name, p.ordering
FROM titles t
JOIN principals p ON p.title_id = t.id
JOIN names n ON p.name_id = n.id
WHERE t.primary_title = 'The Matrix'
  AND t.title_type = 'movie'
  AND p.category_id IN ('actor', 'actress')
ORDER BY p.ordering ASC
