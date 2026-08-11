SELECT primary_title, average_rating
FROM titles
WHERE title_type = 'tvSeries'
  AND num_votes > 10000
  AND average_rating IS NOT NULL
ORDER BY average_rating DESC
