SELECT primary_title, average_rating
FROM titles
WHERE title_type = 'movie'
ORDER BY average_rating DESC
