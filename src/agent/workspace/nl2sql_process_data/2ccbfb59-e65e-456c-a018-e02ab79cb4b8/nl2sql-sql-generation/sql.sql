SELECT
  t.start_year AS year,
  COUNT(*) AS movie_count,
  ROUND(
    (COUNT(*) - LAG(COUNT(*)) OVER (ORDER BY t.start_year)) * 100.0 / NULLIF(LAG(COUNT(*)) OVER (ORDER BY t.start_year), 0),
    2
  ) AS growth_rate
FROM titles t
WHERE t.title_type = 'movie'
  AND t.start_year BETWEEN 2000 AND 2023
  AND t.start_year IS NOT NULL
GROUP BY t.start_year
ORDER BY t.start_year ASC
