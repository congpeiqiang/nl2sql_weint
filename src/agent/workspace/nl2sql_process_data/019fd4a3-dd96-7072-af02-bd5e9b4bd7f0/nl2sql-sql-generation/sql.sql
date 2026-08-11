SELECT FLOOR(start_year / 10) * 10 AS decade,
       AVG(average_rating) AS avg_rating,
       COUNT(*) AS movie_count
FROM titles
WHERE title_type = 'movie' AND start_year IS NOT NULL
GROUP BY FLOOR(start_year / 10) * 10
ORDER BY decade
