SELECT
  d.primary_name AS director_name,
  a.primary_name AS actor_name,
  COUNT(*) AS collaboration_count,
  ROUND(AVG(t.average_rating), 2) AS avg_rating
FROM principals pd
JOIN titles t ON pd.title_id = t.id AND t.title_type = 'movie'
JOIN principals pa ON pa.title_id = t.id
  AND pa.category_id IN ('actor', 'actress')
JOIN names d ON pd.name_id = d.id
JOIN names a ON pa.name_id = a.id
WHERE pd.category_id = 'director'
  AND t.num_votes > 5000
  AND t.average_rating IS NOT NULL
GROUP BY d.id, d.primary_name, a.id, a.primary_name
HAVING COUNT(*) >= 3 AND AVG(t.average_rating) >= 7.5
ORDER BY collaboration_count DESC, avg_rating DESC
