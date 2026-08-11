# 导演-演员黄金搭档分析

## 查询说明
找出合作 3 次以上且合作作品平均评分 >= 7.5 的导演-演员组合。
需要自关联 principals 表：一次查导演，一次查演员。

```sql
SELECT
  d.primary_name AS director_name,
  a.primary_name AS actor_name,
  COUNT(*) AS collaboration_count,
  ROUND(AVG(t.average_rating), 2) AS avg_rating,
  GROUP_CONCAT(DISTINCT t.primary_title ORDER BY t.start_year SEPARATOR '; ') AS movies
FROM principals pd          -- 导演关联
JOIN titles t ON pd.title_id = t.id AND t.title_type = 'movie'
JOIN principals pa ON pa.title_id = t.id  -- 演员关联（同一部作品）
  AND pa.category_id IN ('actor', 'actress')
JOIN names d ON pd.name_id = d.id   -- 导演名
JOIN names a ON pa.name_id = a.id   -- 演员名
WHERE pd.category_id = 'director'
  AND t.num_votes > 5000
  AND t.average_rating IS NOT NULL
GROUP BY d.id, d.primary_name, a.id, a.primary_name
HAVING COUNT(*) >= 3 AND AVG(t.average_rating) >= 7.5
ORDER BY collaboration_count DESC, avg_rating DESC
LIMIT 20
```
