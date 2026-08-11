SELECT g.display_name AS genre, sub.primary_title AS movie_title, sub.average_rating AS rating
FROM (
  SELECT tt.id, tt.primary_title, tt.average_rating, tg.genre_id,
    ROW_NUMBER() OVER (PARTITION BY tg.genre_id ORDER BY tt.average_rating DESC) AS rn
  FROM titles tt
  JOIN titles_genres tg ON tg.title_id = tt.id
  WHERE tt.title_type = 'movie'
    AND tt.average_rating IS NOT NULL
    AND tt.num_votes > 10000
) sub
JOIN genres g ON g.id = sub.genre_id
WHERE sub.rn = 1
ORDER BY g.display_name ASC
