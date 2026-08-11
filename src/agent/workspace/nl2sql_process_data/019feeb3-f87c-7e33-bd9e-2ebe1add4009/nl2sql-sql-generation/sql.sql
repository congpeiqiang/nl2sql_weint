SELECT title_type, COUNT(*) AS work_count
FROM titles
GROUP BY title_type
