SELECT
  t.start_year AS year,
  ROUND(AVG(t.average_rating), 2) AS avg_rating
FROM titles t
WHERE t.title_type = 'tvSeries'
  AND t.start_year BETWEEN 2000 AND 2023
  AND t.start_year IS NOT NULL
  AND t.average_rating IS NOT NULL
GROUP BY t.start_year
ORDER BY t.start_year ASC
