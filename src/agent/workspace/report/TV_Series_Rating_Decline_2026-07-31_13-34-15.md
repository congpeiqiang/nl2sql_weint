# 评分衰退最严重的电视剧系列分析报告

> 生成时间：2026-07-31_13-34-15
> 数据来源：imdb 数据库（title_type='tvSeries'，通过 episodes 表关联各季剧集）

## 1. 概述

本报告分析 imdb 数据库中电视剧系列（`title_type='tvSeries'`）的评分衰退情况。通过 `episodes` 表关联各季剧集，计算每季平均评分，取首季与末季平均评分，计算下降幅度 `(首季评分 - 末季评分) / 首季评分 × 100%`，要求至少 3 季，最终找出评分衰退最严重的 Top 10 电视剧系列。

## 2. 核心数据

### 评分衰退最严重的电视剧系列（Top 10）

| 排名 | 电视剧系列 | 季数 | 首季平均评分 | 末季平均评分 | 下降幅度 |
|:---:|:---|:---:|:---:|:---:|:---:|
| 1 | **The Drew Barrymore Show** | 6 | 6.11 | 2.00 | **67.27%** |
| 2 | **CritiCar** | 3 | 7.52 | 2.55 | **66.09%** |
| 3 | **Royal Histories** | 5 | 7.01 | 2.50 | **64.34%** |
| 4 | **The Beach Hotel** | 3 | 4.06 | 1.51 | **62.81%** |
| 5 | **Nicht nachmachen!** | 3 | 7.82 | 3.00 | **61.64%** |
| 6 | **Slendybob** | 3 | 8.50 | 3.45 | **59.41%** |
| 7 | **The Queen of Flow** | 3 | 8.79 | 3.59 | **59.16%** |
| 8 | **Rainbow High** | 6 | 8.38 | 3.64 | **56.56%** |
| 9 | **Piers Morgan Uncensored** | 3 | 5.35 | 2.40 | **55.14%** |
| 10 | **One Punch Man** | 3 | 8.40 | 3.80 | **54.76%** |

### 图表

![评分衰退最严重的电视剧系列 Top 10](./TV_Series_Rating_Decline_chart.svg)

## 3. 生成 SQL

```sql
-- 分析逻辑：对每个电视剧系列，通过 episodes 表关联各季剧集，
-- 计算每季平均评分，取首季与末季平均评分，计算下降幅度，
-- 要求至少 3 季，按下降幅度降序排列取 Top 10
-- （具体 SQL 由 NL2SQL 专家生成并执行）

WITH season_ratings AS (
    SELECT
      s.id AS series_id,
      s.primary_title AS series_title,
      e.season_number,
      ROUND(AVG(ep.average_rating), 2) AS season_avg_rating
    FROM titles s
    JOIN episodes e ON s.id = e.parent_id
    JOIN titles ep ON e.id = ep.id
    WHERE s.title_type = 'tvSeries'
      AND ep.average_rating IS NOT NULL
      AND e.season_number IS NOT NULL
    GROUP BY s.id, s.primary_title, e.season_number
),
series_stats AS (
    SELECT
      series_id,
      series_title,
      COUNT(*) AS total_seasons,
      MAX(CASE WHEN season_number = (SELECT MIN(season_number) FROM season_ratings sr2 WHERE sr2.series_id = sr.series_id) THEN season_avg_rating END) AS first_season_rating,
      MAX(CASE WHEN season_number = (SELECT MAX(season_number) FROM season_ratings sr2 WHERE sr2.series_id = sr.series_id) THEN season_avg_rating END) AS last_season_rating
    FROM season_ratings sr
    GROUP BY series_id, series_title
    HAVING COUNT(*) >= 3
)
SELECT
    series_title,
    total_seasons,
    first_season_rating,
    last_season_rating,
    ROUND((first_season_rating - last_season_rating) / first_season_rating * 100, 2) AS decline_rate_pct
FROM series_stats
ORDER BY decline_rate_pct DESC
```

## 4. 分析解读

**评分衰退最严重的电视剧系列是《The Drew Barrymore Show》**，共 6 季，首季平均评分 6.11，末季平均评分跌至 2.00，下降幅度高达 **67.27%**。

紧随其后的是《CritiCar》（66.09%）和《Royal Histories》（64.34%）。值得注意的是，多部知名剧集也出现了严重衰退，如《One Punch Man》（54.76%）。

**观察要点：**
- 排名前 10 的剧集下降幅度均超过 54%，说明这些系列在播出过程中评分出现了断崖式下跌。
- 多部剧集首季评分较高（如 Slendybob 8.50、The Queen of Flow 8.79、One Punch Man 8.40），但末季评分跌至 3~4 分区间，衰退幅度惊人。
- 部分剧集（如 The Drew Barrymore Show、Rainbow High）季数较多（6 季），长期播出后评分持续下滑。

> 说明：本查询未额外加 `num_votes > 10000` 过滤（该规则主要针对电影排名类查询），因此结果中包含一些投票数较少的剧集。如需更严格的"高可信评分"分析，可在此基础上增加投票数过滤条件。

## 5. 附录

### 流程追溯

- Phase 1 (Knowledge Loader): 加载业务规则/知识 → 发现 `series_rating_trend.md` 相关 SQL 示例
- Step 2 (Schema Linking): 关联 title、episodes 等表
- Step 3 (Subproblem): 分解为各季平均评分、首末季对比等子问题
- Step 4 (Query Plan): 生成逐步查询计划
- Step 5 (SQL Generation): 生成并 dry_run 校验 SQL
- Step 6 (Execute): 执行成功

### 统计

- 查询耗时：约 39 分钟（其中 SQL 生成 dry_run 阶段耗时较长）
- 是否进入纠错循环：否
