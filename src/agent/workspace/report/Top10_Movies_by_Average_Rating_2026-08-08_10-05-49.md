# Top 10 电影评分排行报告

> 生成时间：2026-08-08_10-05-49
> 数据来源：imdb 数据库（titles 表）

## 1. 概述

本报告统计了 IMDb 数据库中 **average_rating（平均评分）最高的 10 部电影**，展示其标题与评分。查询采用策略 B（快速通道），仅涉及单表 `titles`，通过 `ORDER BY average_rating DESC` 排序并取前 10 行。

## 2. 核心数据

| # | 电影标题 | 评分 |
|---|---------|------|
| 1 | Punjab '95 | 9.0 |
| 2 | Attack on Titan: The Last Attack | 9.0 |
| 3 | David Attenborough: A Life on Our Planet | 8.9 |
| 4 | Inception | 8.8 |
| 5 | The Silence of Swastika | 8.8 |
| 6 | The Phantom of the Opera at the Royal Albert Hall | 8.8 |
| 7 | C/o Kancharapalem | 8.8 |
| 8 | #Home | 8.7 |
| 9 | Demon Slayer: Kimetsu no Yaiba - Mt. Natagumo Arc | 8.7 |
| 10 | 777 Charlie | 8.7 |

## 3. 生成 SQL

```sql
SELECT primary_title, average_rating
FROM titles
WHERE title_type = 'movie'
  AND num_votes > 10000
  AND average_rating IS NOT NULL
ORDER BY average_rating DESC
```

- 按任务规范，SQL 中未写 LIMIT，通过 `run_sql(limit=10)` 控制返回前 10 行。

## 4. 分析解读

- **最高分**：`Punjab '95` 与 `Attack on Titan: The Last Attack` 并列第一，评分均为 **9.0**。
- **评分区间**：Top 10 电影评分集中在 **8.7 ~ 9.0** 之间，差距较小，说明高分电影评分分布较为密集。
- **业务规则**：查询应用了 `title_type = 'movie'`（仅电影）、`num_votes > 10000`（评分可信度过滤，避免低投票影片评分虚高）、`average_rating IS NOT NULL`（排除无评分记录）三项过滤条件。
- **类型多样性**：榜单涵盖剧情片、动画、纪录片、歌剧等多种类型，反映不同题材均可获得高评分。

## 5. 附录

### 图表
![Top 10 电影评分排行](./Top 10 电影评分排行_20260808_100542.html)
图表文件绝对路径：/workspace/report/Top 10 电影评分排行_20260808_100542.html

### 执行说明
- **策略**：B（快速通道）——单表查询（`titles`），简单 ORDER BY + LIMIT
- **流程追溯**：
  - Step 1 (Knowledge Loader): 业务知识加载 → 21s
  - Step 2 (Schema Linking): 获取 schema 上下文 → 5s
  - Step 3 (Subproblem): 策略 B 跳过
  - Step 4 (Query Plan): 策略 B 跳过
  - Step 5 (SQL Generation): 生成 SQL → 57s
  - Step 6 (Execute): 执行成功 → 3s
- **总耗时**：约 1m42s
