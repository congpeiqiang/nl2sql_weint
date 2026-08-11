SELECT COUNT(DISTINCT t.id) AS comedy_movie_count
FROM titles t
JOIN titles_genres tg ON t.id = tg.title_id
JOIN genres g ON tg.genre_id = g.id
WHERE t.title_type = 'movie' AND g.id = 'comedy'
