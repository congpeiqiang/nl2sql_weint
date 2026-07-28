---
nl: 各类型电影平均评分（含热度指数），统计 2000 年后各类型电影的平均评分和热度
sql: |
  SELECT g.display_name AS genre,
    COUNT(DISTINCT t.id) AS movie_count,
    ROUND(AVG(t.average_rating), 2) AS avg_rating,
    ROUND(AVG(t.num_votes), 0) AS avg_votes,
    ROUND(SUM(t.num_votes), 0) AS total_votes,
    ROUND(AVG(t.runtime_minutes), 1) AS avg_runtime
  FROM titles t
  JOIN titles_genres tg ON tg.title_id = t.id
  JOIN genres g ON g.id = tg.genre_id
  WHERE t.title_type = 'movie'
    AND t.num_votes > 5000
    AND t.start_year >= 2000
    AND t.start_year IS NOT NULL
  GROUP BY g.id, g.display_name
  ORDER BY avg_rating DESC
datasource: imdb
tags:
  - 类型
  - 评分
  - 热度
  - ROI
source: seed
---

# 各类型电影平均评分（含热度指数）

## 查询说明
统计 2000 年后各类型电影的平均评分和热度，过滤低投票数影片保证可信度。

## 变体：各类型 ROI 分析
```sql
WITH overall AS (
  SELECT AVG(average_rating) AS global_avg_rating,
         AVG(num_votes) AS global_avg_votes
  FROM titles
  WHERE title_type = 'movie' AND num_votes > 5000 AND start_year >= 2000
)
SELECT g.display_name,
  COUNT(*) AS movie_count,
  ROUND(AVG(t.average_rating), 2) AS avg_rating,
  ROUND(AVG(t.num_votes), 0) AS avg_votes,
  ROUND(
    (AVG(t.average_rating) - o.global_avg_rating) / o.global_avg_rating
    * (AVG(t.num_votes) / o.global_avg_votes),
    4
  ) AS roi_index
FROM titles t
JOIN titles_genres tg ON tg.title_id = t.id
JOIN genres g ON g.id = tg.genre_id
CROSS JOIN overall o
WHERE t.title_type = 'movie' AND t.num_votes > 5000 AND t.start_year >= 2000
GROUP BY g.id, g.display_name, o.global_avg_rating, o.global_avg_votes
ORDER BY roi_index DESC
```
