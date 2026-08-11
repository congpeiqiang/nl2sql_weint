SELECT t.primary_title AS title, t.average_rating AS rating
FROM titles t
WHERE t.title_type = 'movie'
  AND t.num_votes > 10000
  AND t.average_rating IS NOT NULL
ORDER BY t.average_rating DESC
