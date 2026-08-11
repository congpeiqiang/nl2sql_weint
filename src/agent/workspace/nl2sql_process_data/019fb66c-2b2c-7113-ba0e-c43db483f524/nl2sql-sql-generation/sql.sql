SELECT start_year AS year, ROUND(AVG(average_rating), 2) AS avg_rating
FROM titles
WHERE title_type = 'tvSeries'
  AND start_year BETWEEN 2000 AND 2023
  AND start_year IS NOT NULL
  AND average_rating IS NOT NULL
GROUP BY start_year
ORDER BY start_year
