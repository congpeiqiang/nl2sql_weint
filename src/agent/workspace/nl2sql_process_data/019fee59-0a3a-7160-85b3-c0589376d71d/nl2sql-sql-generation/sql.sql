SELECT COUNT(*) AS movie_count
FROM titles
WHERE title_type = 'movie'
  AND start_year >= 2020
