SELECT g.display_name AS genre_name, sub.primary_title AS movie_title, sub.average_rating AS rating
FROM (
    SELECT t.id, t.primary_title, t.average_rating, tg.genre_id,
           ROW_NUMBER() OVER (PARTITION BY tg.genre_id ORDER BY t.average_rating DESC) AS rn
    FROM titles t
    JOIN titles_genres tg ON t.id = tg.title_id
    WHERE t.title_type = 'movie' AND t.average_rating IS NOT NULL AND t.num_votes > 10000
) sub
JOIN genres g ON sub.genre_id = g.id
WHERE sub.rn = 1
ORDER BY g.display_name ASC
