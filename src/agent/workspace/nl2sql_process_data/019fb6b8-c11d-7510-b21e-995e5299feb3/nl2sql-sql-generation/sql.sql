SELECT primary_title, average_rating, num_votes
FROM titles
WHERE title_type = 'movie'
  AND average_rating >= 8.5
  AND num_votes < 5000
ORDER BY average_rating DESC
