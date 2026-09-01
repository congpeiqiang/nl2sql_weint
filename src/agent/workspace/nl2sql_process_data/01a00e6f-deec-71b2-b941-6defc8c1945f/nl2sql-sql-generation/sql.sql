SELECT
  t.primary_title,
  t.start_year,
  t.average_rating,
  t.num_votes,
  ROUND(t.average_rating / LOG10(t.num_votes + 1), 4) AS hidden_gem_score
FROM titles t
WHERE t.title_type = 'movie'
  AND t.average_rating >= 8.5
  AND t.num_votes < 5000
  AND t.average_rating IS NOT NULL
  AND t.start_year IS NOT NULL
ORDER BY t.average_rating DESC, t.num_votes ASC
