WITH season_ratings AS (
  SELECT
    s.id AS series_id,
    s.primary_title AS series_title,
    e.season_number,
    ROUND(AVG(ep.average_rating), 2) AS season_avg_rating
  FROM titles s
  JOIN episodes e ON s.id = e.parent_id
  JOIN titles ep ON e.id = ep.id
  WHERE s.title_type = 'tvSeries'
    AND ep.average_rating IS NOT NULL
    AND e.season_number IS NOT NULL
  GROUP BY s.id, s.primary_title, e.season_number
),
series_stats AS (
  SELECT
    series_id,
    series_title,
    COUNT(*) AS total_seasons,
    MAX(CASE WHEN season_number = (SELECT MIN(season_number) FROM season_ratings sr2 WHERE sr2.series_id = sr.series_id) THEN season_avg_rating END) AS first_season_rating,
    MAX(CASE WHEN season_number = (SELECT MAX(season_number) FROM season_ratings sr2 WHERE sr2.series_id = sr.series_id) THEN season_avg_rating END) AS last_season_rating
  FROM season_ratings sr
  GROUP BY series_id, series_title
  HAVING COUNT(*) >= 3
)
SELECT
  series_title,
  total_seasons,
  first_season_rating,
  last_season_rating,
  ROUND((first_season_rating - last_season_rating) / first_season_rating * 100, 2) AS decline_rate_pct
FROM series_stats
ORDER BY decline_rate_pct DESC
