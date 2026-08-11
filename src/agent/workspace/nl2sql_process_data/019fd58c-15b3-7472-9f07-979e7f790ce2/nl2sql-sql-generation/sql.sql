SELECT
  t.primary_title,
  t.start_year,
  t.average_rating,
  t.num_votes
FROM titles t
WHERE t.title_type = 'movie'
  AND t.average_rating >= 8.0
  AND t.num_votes >= 1000
  AND t.num_votes < 10000
  AND t.average_rating IS NOT NULL
ORDER BY t.average_rating DESC, t.num_votes ASC
