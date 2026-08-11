SELECT t.primary_title, t.runtime_minutes
FROM titles t
WHERE t.title_type = 'movie'
  AND t.runtime_minutes > 180
  AND t.runtime_minutes IS NOT NULL
ORDER BY t.runtime_minutes DESC
