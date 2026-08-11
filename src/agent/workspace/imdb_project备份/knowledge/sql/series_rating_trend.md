# 电视剧系列评分趋势分析

## 查询说明
分析电视剧各季评分变化趋势，计算评分衰退率。
需要 3 层 JOIN：series → episodes → episode_titles。

```sql
WITH season_ratings AS (
  SELECT
    s.id AS series_id,
    s.primary_title AS series_title,
    e.season_number,
    ROUND(AVG(ep.average_rating), 2) AS season_avg_rating,
    COUNT(DISTINCT e.id) AS episode_count
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
    MAX(season_number) AS max_season,
    MIN(season_number) AS min_season,
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
  ROUND((first_season_rating - last_season_rating) / first_season_rating * 100, 2) AS decline_rate_pct,
  CASE
    WHEN (first_season_rating - last_season_rating) / first_season_rating >= 0.3 THEN '严重衰退'
    WHEN (first_season_rating - last_season_rating) / first_season_rating >= 0.15 THEN '明显下滑'
    WHEN (first_season_rating - last_season_rating) / first_season_rating >= 0.05 THEN '轻微下滑'
    WHEN (first_season_rating - last_season_rating) / first_season_rating <= -0.05 THEN '逆势上升'
    ELSE '基本稳定'
  END AS trend_label
FROM series_stats
ORDER BY decline_rate_pct DESC
LIMIT 30
```
