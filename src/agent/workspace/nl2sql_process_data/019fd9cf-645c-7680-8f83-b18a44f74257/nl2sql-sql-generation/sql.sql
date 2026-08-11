SELECT FLOOR(t.start_year / 10) * 10 AS decade,
       AVG(t.average_rating) AS avg_rating,
       COUNT(*) AS movie_count
FROM titles t
WHERE t.title_type = 'movie'
  AND t.start_year IS NOT NULL
  AND t.average_rating IS NOT NULL
GROUP BY FLOOR(t.start_year / 10) * 10
ORDER BY decade ASC
