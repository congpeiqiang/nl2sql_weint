WITH relevant_series AS (
  SELECT id, primary_title
  FROM titles
  WHERE title_type = 'tvSeries' AND num_votes > 10000
),
relevant_episodes AS (
  SELECT e.id, e.parent_id, e.season_number
  FROM episodes e
  JOIN relevant_series rs ON e.parent_id = rs.id
),
season_ratings AS (
  SELECT
    rs.id AS series_id,
    rs.primary_title AS series_title,
    re.season_number,
    AVG(ep.average_rating) AS season_avg_rating
  FROM relevant_series rs
  JOIN relevant_episodes re ON rs.id = re.parent_id
  JOIN titles ep ON re.id = ep.id
  WHERE ep.average_rating IS NOT NULL
    AND re.season_number IS NOT NULL
  GROUP BY rs.id, rs.primary_title, re.season_number
),
season_ranked AS (
  SELECT
    series_id,
    series_title,
    season_number,
    season_avg_rating,
    ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY season_number ASC) AS rn_first,
    ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY season_number DESC) AS rn_last,
    COUNT(*) OVER (PARTITION BY series_id) AS total_seasons
  FROM season_ratings
)
SELECT
  series_title,
  MAX(total_seasons) AS total_seasons,
  MAX(CASE WHEN rn_first = 1 THEN season_avg_rating END) AS first_season_rating,
  MAX(CASE WHEN rn_last = 1 THEN season_avg_rating END) AS last_season_rating,
  ROUND((MAX(CASE WHEN rn_first = 1 THEN season_avg_rating END) - MAX(CASE WHEN rn_last = 1 THEN season_avg_rating END)) / MAX(CASE WHEN rn_first = 1 THEN season_avg_rating END) * 100, 2) AS decline_rate_pct
FROM season_ranked
WHERE total_seasons >= 3
GROUP BY series_id, series_title
ORDER BY decline_rate_pct DESC
