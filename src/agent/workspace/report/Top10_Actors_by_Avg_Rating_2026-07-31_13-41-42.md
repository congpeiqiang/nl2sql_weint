# 参演电影平均评分最高的前 10 位演员分析报告

> 生成时间：2026-07-31_13-41-42
> 数据来源：imdb 数据库（names / principals / titles 表）

## 1. 概述

本报告分析 imdb 数据库中参演电影平均评分最高的前 10 位演员。筛选条件为：**参演电影数 ≥ 5** 且 **每部电影投票数 > 50000**，按平均评分降序排列取前 10。

**查询逻辑说明：**
- **演员定义**：`category_id IN ('actor', 'actress')`（同时包含男、女演员）
- **电影筛选**：`title_type = 'movie'` 且 `num_votes > 50000`（每部电影投票数 > 50000）
- **参演电影数**：`COUNT(DISTINCT p.title_id) >= 5`（HAVING 过滤）
- **平均评分**：`AVG(t.average_rating)`，按降序排列取前 10

## 2. 核心数据

| 排名 | 演员 | 平均评分 | 参演电影数 |
|:---:|------|:---:|:---:|
| 1 | Leonardo DiCaprio | 7.76 | 7 |
| 2 | Zendaya | 7.73 | 6 |
| 3 | Mark Ruffalo | 7.60 | 6 |
| 4 | Domhnall Gleeson | 7.57 | 7 |
| 5 | Marion Cotillard | 7.56 | 5 |
| 6 | Timothée Chalamet | 7.50 | 6 |
| 7 | Hugh Jackman | 7.44 | 8 |
| 8 | Marisa Tomei | 7.42 | 5 |
| 9 | John Carroll Lynch | 7.40 | 7 |
| 10 | Christopher Plummer | 7.38 | 5 |

## 3. 图表

![参演电影平均评分最高的前10位演员](./Top10_Actors_by_Avg_Rating_chart.svg)

## 4. 生成 SQL

```sql
SELECT n.primary_name AS actor_name,
  ROUND(AVG(t.average_rating), 2) AS avg_rating,
  COUNT(DISTINCT p.title_id) AS movie_count
FROM names n
JOIN principals p ON p.name_id = n.id
JOIN titles t ON t.id = p.title_id
WHERE p.category_id IN ('actor', 'actress')
  AND t.title_type = 'movie'
  AND t.num_votes > 50000
GROUP BY n.id, n.primary_name
HAVING COUNT(DISTINCT p.title_id) >= 5
ORDER BY AVG(t.average_rating) DESC
```

## 5. 分析解读

- **Leonardo DiCaprio** 以平均评分 **7.76** 位居榜首，参演 7 部高投票电影，是榜单中评分最高的演员。
- **Zendaya**（7.73）和 **Mark Ruffalo**（7.60）紧随其后，分列第二、三名。
- **Hugh Jackman** 参演电影数最多（8 部），但平均评分（7.44）排名第 7，说明其作品数量多但评分相对分散。
- 榜单前 10 位演员的平均评分集中在 **7.38 ~ 7.76** 区间，差距较小，竞争激烈。
- 榜单中既有老牌影星（Leonardo DiCaprio、Hugh Jackman、Christopher Plummer），也有新生代演员（Zendaya、Timothée Chalamet），体现了不同代际演员的优质作品表现。

## 6. 附录

- **数据来源**：imdb 数据库
- **查询工具**：nl2sql 子智能体（sql-of-thought 技能）
- **图表类型**：水平柱状图（BarChart, orientation="horizontal"）
- **图表文件**：`Top10_Actors_by_Avg_Rating_chart.svg`
