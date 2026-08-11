SELECT DISTINCT t.primary_title AS title, t.average_rating
FROM titles t
JOIN titleakas ta ON t.id = ta.title_id
WHERE t.title_type = 'movie'
  AND ta.region = 'CA'
  AND t.average_rating IS NOT NULL
  AND t.num_votes > 10000
ORDER BY t.average_rating DESC
