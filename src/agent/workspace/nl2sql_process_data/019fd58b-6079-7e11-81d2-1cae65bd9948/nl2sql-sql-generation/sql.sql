WITH season_ratings AS (
  SELECT
    s.id AS series_id,
    s.primary_title AS series_title,
    e.season_number,
    AVG(ep.average_rating) AS season_avg_rating
  FROM titles s
  JOIN episodes e ON s.id = e.parent_id
  JOIN titles ep ON e.id = ep.id
  WHERE s.title_type = 'tvSeries'
    AND ep.average_rating IS NOT NULL
    AND e.season_number IS NOT NULL
    AND ep.num_votes > 10000
  GROUP BY s.id, s.primary_title, e.season_number
),
series_first_season AS (
  SELECT series_id, MIN(season_number) AS first_season
  FROM season_ratings
  GROUP BY series_id
)
SELECT
  sr.series_title,
  COUNT(DISTINCT sr.season_number) AS total_seasons,
  ROUND(AVG(CASE WHEN sr.season_number = fs.first_season THEN sr.season_avg_rating END), 2) AS first_season_avg,
  ROUND(AVG(CASE WHEN sr.season_number > fs.first_season THEN sr.season_avg_rating END), 2) AS subsequent_seasons_avg,
  ROUND(
    AVG(CASE WHEN sr.season_number = fs.first_season THEN sr.season_avg_rating END)
    - AVG(CASE WHEN sr.season_number > fs.first_season THEN sr.season_avg_rating END),
    2
  ) AS decline_magnitude
FROM season_ratings sr
JOIN series_first_season fs ON sr.series_id = fs.series_id
GROUP BY sr.series_id, sr.series_title
HAVING COUNT(DISTINCT sr.season_number) >= 2
ORDER BY decline_magnitude DESC
