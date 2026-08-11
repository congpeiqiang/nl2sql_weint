# Comedy 类型电影数量统计报告

> 生成时间：2026-08-05_10-20-15
> 数据来源：imdb 数据库

## 1. 概述

本报告统计了 IMDb 数据库中 **Comedy（喜剧）** 类型的电影数量。查询通过 `titles`、`titles_genres`、`genres` 三表关联，筛选出 `title_type='movie'` 且类型为 `comedy` 的电影记录。

## 2. 核心数据

| 指标 | 数值 |
|------|------|
| Comedy 类型电影数量 | **66,885 部** |

![Comedy 类型电影数量](./Comedy_Movie_Count_chart.svg)

## 3. 生成 SQL

```sql
SELECT COUNT(DISTINCT t.id) AS comedy_movie_count
FROM titles t
JOIN titles_genres tg ON t.id = tg.title_id
JOIN genres g ON tg.genre_id = g.id
WHERE t.title_type = 'movie' AND g.id = 'comedy'
```

## 4. 分析解读

- **Comedy 类型电影共 66,885 部**，是 IMDb 数据库中数量庞大的电影类型之一。
- 查询使用了 `COUNT(DISTINCT t.id)` 确保每部电影只计数一次，避免因多类型关联导致的重复计数。
- 通过 `titles_genres` 关联表建立 `titles` 与 `genres` 之间的多对多关系，准确筛选出 Comedy 类型。

## 5. 附录

### 执行流程

- Step 1 (Knowledge Loader): 加载业务知识，确认 `title_type='movie'` 为电影、`genres.id` 存字符串、多表 JOIN 路径
- Step 2 (Schema Linking): 裁剪出相关表 `titles`、`titles_genres`、`genres` 及 JOIN 关系
- Step 3 (Subproblem): 分解为 SELECT（COUNT）、FROM、JOIN、WHERE 子句
- Step 4 (Query Plan): 生成逐步查询计划
- Step 5 (SQL Generation): 生成 SQL 并通过 dry_run 验证
- Step 5.5 (Performance Optimization): 检查无性能隐患
- Step 6 (Execute): 执行查询成功
