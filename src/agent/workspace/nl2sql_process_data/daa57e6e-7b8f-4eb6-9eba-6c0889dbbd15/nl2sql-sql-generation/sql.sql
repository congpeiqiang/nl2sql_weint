WITH yearly_counts AS (
  SELECT
    t.start_year AS year,
    COUNT(DISTINCT t.id) AS movie_count
  FROM titles t
  WHERE t.title_type = 'movie'
    AND t.start_year BETWEEN 2000 AND 2023
    AND t.start_year IS NOT NULL
  GROUP BY t.start_year
)
SELECT
  year,
  movie_count,
  ROUND(
    (movie_count - LAG(movie_count) OVER (ORDER BY year)) * 100.0 / NULLIF(LAG(movie_count) OVER (ORDER BY year), 0),
    2
  ) AS growth_rate
FROM yearly_counts
ORDER BY year ASC
