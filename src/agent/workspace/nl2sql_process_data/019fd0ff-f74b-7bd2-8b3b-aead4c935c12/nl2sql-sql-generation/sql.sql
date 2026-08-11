SELECT primary_title, runtime_minutes
FROM titles
WHERE title_type = 'movie' AND runtime_minutes > 180
ORDER BY runtime_minutes DESC
