SELECT AVG(t.average_rating) AS avg_rating
FROM titles t
WHERE t.title_type = 'movie'
  AND t.start_year BETWEEN 1990 AND 1999
  AND t.start_year IS NOT NULL
  AND t.average_rating IS NOT NULL
