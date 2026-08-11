WITH ranked AS (
  SELECT 
    g.display_name AS genre,
    t.primary_title AS movie_title,
    t.average_rating AS rating,
    ROW_NUMBER() OVER (PARTITION BY g.id, g.display_name ORDER BY t.average_rating DESC) AS rn
  FROM titles t
  JOIN titles_genres tg ON tg.title_id = t.id
  JOIN genres g ON g.id = tg.genre_id
  WHERE t.title_type = 'movie'
    AND t.num_votes > 10000
    AND t.average_rating IS NOT NULL
)
SELECT genre, movie_title, rating
FROM ranked
WHERE rn = 1
ORDER BY genre ASC
