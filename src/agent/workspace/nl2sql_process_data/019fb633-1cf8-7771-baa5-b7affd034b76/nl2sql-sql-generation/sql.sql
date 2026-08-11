SELECT COUNT(*) AS movie_count
FROM titles
WHERE title_type = 'movie' AND num_votes > 1000000
