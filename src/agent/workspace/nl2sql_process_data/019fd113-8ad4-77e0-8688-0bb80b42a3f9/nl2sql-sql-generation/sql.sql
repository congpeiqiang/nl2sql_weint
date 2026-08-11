SELECT ROUND(AVG(t.average_rating), 2) AS avg_rating
FROM titles t
JOIN principals p ON p.title_id = t.id
JOIN names n ON p.name_id = n.id
WHERE n.primary_name = 'Christopher Nolan'
  AND p.category_id = 'director'
  AND t.title_type = 'movie'
