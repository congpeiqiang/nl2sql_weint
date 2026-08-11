SELECT title_type, COUNT(*) AS movie_count
FROM titles
GROUP BY title_type
ORDER BY movie_count DESC
