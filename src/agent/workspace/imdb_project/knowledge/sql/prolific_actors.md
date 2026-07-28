---
nl: 高产演员分析（含多面手得分），找出参演高分电影最多的演员，同时计算角色多样性
sql: |
  SELECT n.primary_name,
    COUNT(DISTINCT p.title_id) AS movie_count,
    ROUND(AVG(t.average_rating), 2) AS avg_movie_rating,
    COUNT(DISTINCT p.category_id) AS role_diversity,
    MIN(t.start_year) AS first_movie_year,
    MAX(t.start_year) AS last_movie_year,
    MAX(t.start_year) - MIN(t.start_year) AS career_span
  FROM names n
  JOIN principals p ON p.name_id = n.id
  JOIN titles t ON t.id = p.title_id
  WHERE p.category_id IN ('actor', 'actress')
    AND t.title_type = 'movie'
    AND t.average_rating > 7.0
    AND t.num_votes > 10000
    AND t.start_year IS NOT NULL
  GROUP BY n.id, n.primary_name
  HAVING COUNT(DISTINCT p.title_id) >= 10
  ORDER BY movie_count DESC
  LIMIT 20
datasource: imdb
tags:
  - 演员
  - 高产
  - 多面手
  - 导演
source: seed
---

# 高产演员分析（含多面手得分）

## 查询说明
找出参演高分电影最多的演员，同时计算角色多样性（多面手得分）。

## 变体：高产导演
```sql
SELECT n.primary_name,
  COUNT(DISTINCT p.title_id) AS directed_count,
  ROUND(AVG(t.average_rating), 2) AS avg_rating,
  ROUND(SUM(t.num_votes), 0) AS total_votes,
  ROUND(AVG(t.runtime_minutes), 1) AS avg_runtime
FROM names n
JOIN principals p ON p.name_id = n.id AND p.category_id = 'director'
JOIN titles t ON p.title_id = t.id
WHERE t.title_type = 'movie' AND t.start_year IS NOT NULL
GROUP BY n.id, n.primary_name
HAVING COUNT(DISTINCT p.title_id) >= 5
ORDER BY avg_rating DESC
LIMIT 20
```
