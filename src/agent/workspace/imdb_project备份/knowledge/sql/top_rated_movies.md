# 高分电影 Top N（含贝叶斯校正）

## 查询说明
使用贝叶斯加权评分校正低投票数偏差，得到更可靠的高分电影排名。

```sql
WITH global_avg AS (
  SELECT AVG(average_rating) AS avg_rating FROM titles
  WHERE title_type = 'movie' AND num_votes > 1000
)
SELECT t.primary_title,
  t.start_year,
  t.average_rating,
  t.num_votes,
  ROUND(
    (t.num_votes * t.average_rating + 1000 * g.avg_rating) / (t.num_votes + 1000),
    4
  ) AS bayesian_rating
FROM titles t
CROSS JOIN global_avg g
WHERE t.title_type = 'movie'
  AND t.num_votes > 1000
  AND t.start_year >= 2000
  AND t.average_rating IS NOT NULL
ORDER BY bayesian_rating DESC
LIMIT 20
```

## 变体：各年代最佳影片
```sql
WITH ranked AS (
  SELECT primary_title, start_year, average_rating, num_votes,
    FLOOR(start_year / 10) * 10 AS decade,
    ROW_NUMBER() OVER (PARTITION BY FLOOR(start_year / 10) * 10 ORDER BY average_rating DESC) AS rn
  FROM titles
  WHERE title_type = 'movie' AND num_votes > 50000 AND start_year IS NOT NULL
)
SELECT decade AS decade_range, primary_title, start_year, average_rating, num_votes
FROM ranked
WHERE rn = 1
ORDER BY decade_range DESC
```
