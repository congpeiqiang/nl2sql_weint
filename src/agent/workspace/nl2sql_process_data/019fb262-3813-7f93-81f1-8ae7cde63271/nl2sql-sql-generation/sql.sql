SELECT primary_title AS title, average_rating
FROM titles
WHERE title_type = 'movie'
  AND average_rating IS NOT NULL
  AND num_votes > 10000
ORDER BY average_rating DESC