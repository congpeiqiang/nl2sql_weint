SELECT g.display_name AS genre,
       COUNT(DISTINCT t.id) AS movie_count
FROM titles t
JOIN titles_genres tg ON tg.title_id = t.id
JOIN genres g ON g.id = tg.genre_id
WHERE t.title_type = 'movie'
GROUP BY g.id, g.display_name
ORDER BY movie_count DESC
