# Top 10 电影评分排行报告

> 生成时间：2026-08-08_08-55-58
> 数据来源：imdb 数据库（titles 表）

## 1. 概述

本报告基于 IMDb 数据库，查询 **average_rating（平均评分）最高的 10 部电影**，展示其标题（primary_title）与评分（average_rating）。作为排名类查询，应用了 `num_votes > 10000` 过滤以保证评分可信度（低投票数影片评分不可靠）。

## 2. 核心数据

| # | 标题 (primary_title) | 评分 (average_rating) |
|---|----------------------|----------------------|
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

- **最高分**：`Punjab '95` 与 `Attack on Titan: The Last Attack` 并列第一，评分均为 **9.0**。
- **评分区间**：Top 10 影片评分集中在 **8.7 ~ 9.0** 之间，差距极小（仅 0.3 分），说明头部影片质量非常接近。
- **类型分布**：榜单涵盖剧情片、动画、纪录片、音乐剧等多种类型，其中动画类（《进击的巨人》《鬼灭之刃》）占据两席。
- **可信度说明**：所有影片均满足 `num_votes > 10000` 的投票数门槛，评分具有统计可信度。

## 5. 附录

- 图表：Top 10 电影评分柱状图（已渲染为交互式 HTML 图表）
- 数据行数：10 行
- 查询数据库：imdb
