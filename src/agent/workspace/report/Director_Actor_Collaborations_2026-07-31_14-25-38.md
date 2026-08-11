# 合作 3 次以上且平均评分 ≥ 7.5 的导演-演员组合分析报告

> 生成时间：2026-07-31_14-25-38
> 数据来源：imdb 数据库（principals / titles / names 表）

## 1. 概述

本报告分析 imdb 数据库中**合作 3 次以上**且**合作电影平均评分 ≥ 7.5** 的导演-演员组合。通过 `principals` 表自关联实现导演-演员配对，统计各组合的合作次数与平均评分，并筛选出符合条件的高质量长期合作搭档。

**核心结论：** 共筛选出 50 条符合条件的导演-演员组合（前 50 条展示），其中 **S.K. Bhagavan × Rajkumar** 以 17 次合作、平均评分 8.43 位居榜首。

## 2. 核心数据

### 2.1 Top 10 导演-演员组合（按合作次数降序）

| 导演 | 演员 | 合作次数 | 平均评分 |
|------|------|:---:|:---:|
| S.K. Bhagavan | Rajkumar | 17 | 8.43 |
| B. Dorai Raj | Rajkumar | 16 | 8.42 |
| S.K. Bhagavan | K.S. Ashwath | 11 | 7.72 |
| B. Dorai Raj | K.S. Ashwath | 11 | 7.72 |
| V. Somasekhar | Rajkumar | 9 | 8.60 |
| Ashok Pati | Babushan Mohanty | 9 | 7.54 |
| Takashi Ōtsuka | Orie Kimoto | 9 | 7.53 |
| Takashi Ōtsuka | Atsuko Enomoto | 9 | 7.53 |
| T.V. Singh Thakore | Rajkumar | 8 | 8.41 |
| Sivaram Peketi | Rajkumar | 8 | 8.38 |

### 2.2 图表可视化

![合作3次以上且平均评分≥7.5的导演-演员组合（Top 10）](./Director_Actor_Collaborations_chart.svg)

## 3. 生成 SQL

```sql
SELECT
  d.primary_name AS director_name,
  a.primary_name AS actor_name,
  COUNT(*) AS collaboration_count,
  ROUND(AVG(t.average_rating), 2) AS avg_rating
FROM principals pd
JOIN titles t ON pd.title_id = t.id AND t.title_type = 'movie'
JOIN principals pa ON pa.title_id = t.id
  AND pa.category_id IN ('actor', 'actress')
JOIN names d ON pd.name_id = d.id
JOIN names a ON pa.name_id = a.id
WHERE pd.category_id = 'director'
  AND t.average_rating IS NOT NULL
GROUP BY d.id, d.primary_name, a.id, a.primary_name
HAVING COUNT(*) >= 3 AND AVG(t.average_rating) >= 7.5
ORDER BY collaboration_count DESC, avg_rating DESC
```

**关键逻辑：**
- **导演**：`principals.category_id = 'director'`
- **演员**：`principals.category_id IN ('actor', 'actress')`（同时包含男女演员）
- **电影**：`titles.title_type = 'movie'`
- **合作次数**：按导演+演员分组后 `COUNT(*)`
- **平均评分**：`AVG(titles.average_rating)`，过滤条件 `>= 7.5`
- **HAVING** 过滤合作次数 `>= 3`

## 4. 分析解读

### 4.1 高合作频次组合
- **S.K. Bhagavan × Rajkumar**（17 次，8.43 分）和 **B. Dorai Raj × Rajkumar**（16 次，8.42 分）是合作最频繁的组合，且评分均超过 8.4，属于高质量长期搭档。
- 演员 **Rajkumar** 出现在多个高评分组合中（S.K. Bhagavan、B. Dorai Raj、V. Somasekhar、T.V. Singh Thakore、Sivaram Peketi），说明其与多位导演保持了稳定的高质量合作。

### 4.2 平均评分表现
- **V. Somasekhar × Rajkumar** 以 **8.60** 的平均评分位居评分榜首，虽然合作次数为 9 次，但质量极高。
- 所有入选组合的平均评分均 ≥ 7.5，符合筛选条件。

### 4.3 组合特征
- 入选组合多为**区域性电影工业**（如卡纳达语、奥里亚语、日语）的长期搭档，体现了特定市场内导演与演员的稳定合作关系。

## 5. 附录

### 5.1 查询执行流程

- Phase 1 (Knowledge Loader): 查询业务规则和知识库
- Step 2 (Schema Linking): 识别所需表/列/关系（principals、titles、names）
- Step 3 (Subproblem): 分解子问题（导演识别、演员识别、合作统计、评分聚合）
- Step 4 (Query Plan): 生成查询计划
- Step 5 (SQL Generation): 生成 SQL 并 dry_run 验证
- Step 6 (Execute): 成功执行（耗时约 1 小时 23 分钟）

### 5.2 说明

- 结果共返回 50 条（前 50 条展示），完整结果可通过调整 `limit` 参数获取。
- 数据基于 imdb 数据库，评分字段为 `titles.average_rating`。
