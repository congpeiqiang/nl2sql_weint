WITH ranked AS (
  SELECT 
    g.display_name AS genre_name,
    t.primary_title AS movie_title,
    t.average_rating AS rating,
    ROW_NUMBER() OVER (PARTITION BY tg.genre_id ORDER BY t.average_rating DESC) AS rn
  FROM titles t
  JOIN titles_genres tg ON tg.title_id = t.id
  JOIN genres g ON g.id = tg.genre_id
  WHERE t.title_type = 'movie'
    AND t.average_rating IS NOT NULL
    AND t.num_votes > 10000
)
SELECT genre_name, movie_title, rating
FROM ranked
WHERE rn = 1
ORDER BY genre_name ASC
