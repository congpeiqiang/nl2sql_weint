SELECT
  d.primary_name AS director_name,
  a.primary_name AS actor_name,
  pairs.cnt AS collaboration_count,
  pairs.avg_rating
FROM (
  SELECT pd.name_id AS director_id, pa.name_id AS actor_id, COUNT(*) AS cnt, ROUND(AVG(t.average_rating), 2) AS avg_rating
  FROM principals pd
  JOIN titles t ON pd.title_id = t.id AND t.title_type = 'movie' AND t.num_votes > 5000 AND t.average_rating IS NOT NULL
  JOIN principals pa ON pa.title_id = t.id AND pa.category_id IN ('actor', 'actress')
  WHERE pd.category_id = 'director'
  GROUP BY pd.name_id, pa.name_id
  HAVING COUNT(*) >= 3 AND AVG(t.average_rating) >= 7.5
) pairs
JOIN names d ON pairs.director_id = d.id
JOIN names a ON pairs.actor_id = a.id
ORDER BY pairs.cnt DESC, pairs.avg_rating DESC
