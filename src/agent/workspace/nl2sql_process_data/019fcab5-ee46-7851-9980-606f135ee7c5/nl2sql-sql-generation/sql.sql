SELECT ROUND(AVG(t.average_rating), 2) AS avg_rating
FROM titles t
WHERE t.title_type = 'movie'
  AND t.start_year IS NOT NULL
  AND t.start_year >= 1990
  AND t.start_year <= 1999