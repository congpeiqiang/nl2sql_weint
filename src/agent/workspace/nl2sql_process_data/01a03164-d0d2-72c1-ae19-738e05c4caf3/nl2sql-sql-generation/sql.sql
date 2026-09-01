SELECT n.primary_name AS actor_name,
       COUNT(DISTINCT tg.genre_id) AS genre_count
FROM names n
JOIN principals p ON p.name_id = n.id
JOIN titles t ON p.title_id = t.id
JOIN titles_genres tg ON t.id = tg.title_id
WHERE p.category_id IN ('actor', 'actress')
  AND t.title_type = 'movie'
  AND t.num_votes > 10000
GROUP BY n.id, n.primary_name
ORDER BY genre_count DESC, n.primary_name ASC
