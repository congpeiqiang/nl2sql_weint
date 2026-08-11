SELECT g.id AS genre, COUNT(tg.title_id) AS movie_count
FROM titles_genres tg
JOIN genres g ON tg.genre_id = g.id
GROUP BY g.id
ORDER BY movie_count DESC
