# 1990 年代（1990-1999）电影平均评分分析报告（策略B · 快速通道）

> 生成时间：2026-08-03 16:28:12
> 数据来源：IMDb 数据库（imdb）

## 1. 概述

本报告分析 1990 年代（1990-1999）上映电影的平均评分情况。本次查询采用**策略B（快速通道）**，按规范跳过了知识预加载（knowledge-loader）和性能优化（performance-optimization）环节，直接完成 schema 识别 → SQL 生成 → 执行，执行速度更快。

## 2. 核心数据

**1990 年代（1990-1999）电影的平均评分：6.15**（精确值：6.153）

| 指标 | 数值 |
|------|------|
| 平均评分 | 6.15 |
| 精确值 | 6.153 |
| 时间范围 | 1990 - 1999 |
| 电影类型 | movie |

### 图表展示

![1990年代电影平均评分（策略B）](./0_20260803_162814.svg)

## 3. 生成 SQL

```sql
SELECT AVG(average_rating) AS avg_rating
FROM titles
WHERE title_type = 'movie'
  AND start_year BETWEEN 1990 AND 1999
```

## 4. 分析解读

- **平均评分 6.15**：1990 年代电影的平均评分为 6.15 分（满分 10 分），处于中等偏上水平。
- **策略B（快速通道）**：本次查询跳过了知识预加载和性能优化环节，直接进行 schema 识别、SQL 生成与执行，执行效率更高。
- **数据过滤**：查询限定为 `title_type='movie'`（电影），`start_year` 在 1990 至 1999 年之间。
- **单表聚合**：仅涉及 `titles` 单表，通过 AVG 聚合函数直接计算，性能高效。

## 5. 附录

### 执行过程（策略B · 快速通道）

| 步骤 | 状态 |
|------|------|
| schema-linking | ✅ 完成 |
| subproblem | ✅ 完成 |
| query-plan | ✅ 完成 |
| sql-generation | ✅ 完成（dry_run 验证通过） |
| run_sql | ✅ 完成 |

> 注：策略B跳过了 knowledge-loader（知识预加载）和 performance-optimization（性能优化）环节。

### 涉及字段

- `titles.title_type` — 标题类型（过滤 movie）
- `titles.start_year` — 上映年份（过滤 1990-1999）
- `titles.average_rating` — 平均评分（聚合计算）
