SELECT AVG(average_rating) AS avg_rating
FROM titles
WHERE title_type = 'movie'
  AND start_year >= 1990
  AND start_year <= 1999