SELECT
  n.primary_name AS actor_name,
  MAX(t.start_year) - MIN(t.start_year) AS career_span
FROM names n
JOIN principals p ON p.name_id = n.id
JOIN titles t ON p.title_id = t.id
WHERE p.category_id IN ('actor', 'actress')
  AND t.start_year IS NOT NULL
GROUP BY n.id, n.primary_name
ORDER BY career_span DESC
