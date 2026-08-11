# Top 10 Movies by Average Rating — 平均评分最高的 10 部电影

> 生成时间：2026-07-31_16-56-35
> 数据来源：imdb 数据库（titles 表）

## 1. 概述

本报告统计了 IMDb 数据库中 **average_rating（平均评分）最高** 的 10 部电影，展示其标题与评分。查询基于 `titles` 表，筛选 `title_type = 'movie'`，按 `average_rating DESC` 排序取前 10。

> 说明：根据 IMDb 评分可信度规则，排名类查询添加了 `num_votes > 10000` 过滤，确保评分具有较高可信度（避免低投票数电影因少数人评分而虚高）。

## 2. 核心数据

| 排名 | 标题 | 评分 |
|:---:|------|:---:|
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

## 3. 图表

![Top 10 Movies by Average Rating](./Top10_Movies_by_Average_Rating_chart.svg)

## 4. 分析解读

- **最高分**：`Punjab '95` 与 `Attack on Titan: The Last Attack` 并列第一，评分均为 **9.0**。
- **评分区间**：Top 10 电影的评分集中在 **8.7 ~ 9.0** 之间，差距较小（仅 0.3 分）。
- **并列现象**：8.8 分有 4 部电影并列，8.7 分有 3 部电影并列，说明高分电影评分分布较为密集。
- **题材多样性**：榜单涵盖剧情片（Punjab '95）、动画（Attack on Titan、Demon Slayer）、纪录片（David Attenborough）、科幻（Inception）等多种类型。

## 5. 附录

### 生成 SQL

```sql
SELECT title, average_rating
FROM titles
WHERE title_type = 'movie'
  AND num_votes > 10000
ORDER BY average_rating DESC
```

### 执行说明

- **策略**：策略B（快速通道）— 单表简单查询（ORDER BY + LIMIT）
- **验证**：已通过 `dry_run` 验证 SQL 有效性后执行
- **行数控制**：通过 `run_sql` 的 `limit=10` 参数控制返回行数
