SELECT t.primary_title, t.average_rating, t.num_votes
FROM titles t
WHERE t.title_type = 'movie'
  AND t.average_rating >= 8.5
  AND t.num_votes < 5000
ORDER BY t.average_rating DESC
