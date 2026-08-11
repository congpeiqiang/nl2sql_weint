SELECT g.display_name AS genre, COUNT(DISTINCT t.id) AS movie_count
FROM titles t
JOIN titles_genres tg ON t.id = tg.title_id
JOIN genres g ON tg.genre_id = g.id
WHERE t.title_type = 'movie'
GROUP BY g.display_name
ORDER BY movie_count DESC
