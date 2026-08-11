SELECT primary_title, runtime_minutes
FROM titles
WHERE title_type = 'movie' AND runtime_minutes > 180 AND runtime_minutes IS NOT NULL
ORDER BY runtime_minutes DESC
