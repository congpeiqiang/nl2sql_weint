SELECT FLOOR(t.start_year / 10) * 10 AS decade,
       ROUND(AVG(t.average_rating), 2) AS avg_rating,
       COUNT(*) AS movie_count
FROM titles t
WHERE t.title_type = 'movie'
  AND t.start_year IS NOT NULL
GROUP BY FLOOR(t.start_year / 10) * 10
ORDER BY decade ASC
