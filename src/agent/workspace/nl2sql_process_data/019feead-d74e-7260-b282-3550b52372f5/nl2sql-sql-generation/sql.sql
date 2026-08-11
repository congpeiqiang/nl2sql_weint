SELECT title_type, COUNT(*) AS title_count
FROM titles
GROUP BY title_type
ORDER BY title_count DESC
