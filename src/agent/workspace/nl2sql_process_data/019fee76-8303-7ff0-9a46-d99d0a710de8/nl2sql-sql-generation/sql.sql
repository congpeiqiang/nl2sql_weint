SELECT t.primary_title AS 片名, t.average_rating AS 评分
FROM titles t
WHERE t.title_type = 'movie'
  AND t.num_votes > 10000
  AND t.average_rating IS NOT NULL
ORDER BY t.average_rating DESC
