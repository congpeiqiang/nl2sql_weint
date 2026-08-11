# Top 10 电影平均评分分析报告

> 生成时间：2026-07-31_10-34-07
> 数据来源：imdb 数据库（titles 表）

## 1. 概述

本报告基于 imdb 数据库，查询 **average_rating（平均评分）最高的 10 部电影**，展示其标题与评分。查询采用快速通道策略（策略B），针对单表简单查询（ORDER BY + LIMIT）进行优化。

**业务规则应用：**
- `title_type = 'movie'` — 仅查询电影（排除电视剧、短片等）
- `num_votes > 10000` — 评分可信度规则，排名类查询过滤低投票数影片
- `average_rating IS NOT NULL` — 排除无评分记录

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

## 3. 生成 SQL

```sql
SELECT primary_title, average_rating
FROM titles
WHERE title_type = 'movie'
  AND num_votes > 10000
  AND average_rating IS NOT NULL
ORDER BY average_rating DESC
```

## 4. 分析解读

- **最高评分**：Punjab '95 与 Attack on Titan: The Last Attack 并列第一，评分均为 **9.0**。
- **评分区间**：Top 10 电影的评分集中在 **8.7 ~ 9.0** 之间，差距较小（0.3 分）。
- **并列情况**：第 4~7 名（Inception、The Silence of Swastika、The Phantom of the Opera at the Royal Albert Hall、C/o Kancharapalem）评分均为 8.8；第 8~10 名（#Home、Demon Slayer、777 Charlie）评分均为 8.7。
- **类型多样性**：榜单涵盖剧情片、动画、纪录片、音乐剧等多种类型，反映不同题材影片均能获得高评分。

### 图表

![Top 10 Movies by Average Rating](./Top10_Movies_by_Average_Rating_chart.svg)

## 5. 附录

- **查询策略**：策略B（快速通道）— 单表简单查询，跳过 schema-linking、subproblem、query-plan 阶段，直接生成 SQL。
- **执行结果**：成功返回 10 行数据。
- **图表类型**：水平柱状图（BarChart, orientation="horizontal"），Y 轴为电影标题，X 轴为平均评分。
