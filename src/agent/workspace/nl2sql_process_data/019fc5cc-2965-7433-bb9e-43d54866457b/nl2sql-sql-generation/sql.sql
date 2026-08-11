SELECT t.primary_title AS series_title, COUNT(DISTINCT e.id) AS episode_count
FROM titles t
JOIN episodes e ON e.parent_id = t.id
WHERE t.title_type = 'tvSeries'
GROUP BY t.id, t.primary_title
ORDER BY episode_count DESC
