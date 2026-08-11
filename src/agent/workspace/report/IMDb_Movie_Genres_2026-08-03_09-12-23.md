# IMDb 电影类型数量分析报告

> 生成时间：2026-08-03_09-12-23
> 数据来源：imdb 数据库（genres 维度表）

## 1. 概述

本报告统计了 IMDb 数据库中电影类型（genres）的总数，并列出全部类型名称。该查询为单表简单查询，仅涉及 `genres` 维度表，无需多表 JOIN 或复杂聚合。

**核心结论：IMDb 数据库中共有 28 种电影类型。**

## 2. 核心数据

| # | 类型 ID | 类型名称 |
|---|---------|----------|
| 1 | action | Action |
| 2 | adult | Adult |
| 3 | adventure | Adventure |
| 4 | animation | Animation |
| 5 | biography | Biography |
| 6 | comedy | Comedy |
| 7 | crime | Crime |
| 8 | documentary | Documentary |
| 9 | drama | Drama |
| 10 | family | Family |
| 11 | fantasy | Fantasy |
| 12 | film-noir | Film-Noir |
| 13 | game-show | Game-Show |
| 14 | history | History |
| 15 | horror | Horror |
| 16 | music | Music |
| 17 | musical | Musical |
| 18 | mystery | Mystery |
| 19 | news | News |
| 20 | reality-tv | Reality-TV |
| 21 | romance | Romance |
| 22 | sci-fi | Sci-Fi |
| 23 | short | Short |
| 24 | sport | Sport |
| 25 | talk-show | Talk-Show |
| 26 | thriller | Thriller |
| 27 | war | War |
| 28 | western | Western |

## 3. 生成 SQL

```sql
SELECT g.id AS genre_id,
       g.display_name AS genre_name
FROM genres g
ORDER BY g.id
```

该 SQL 已通过 `dry_run` 验证，并成功执行返回 28 行数据。

## 4. 分析解读

### 图表

![IMDb 电影类型分布](./IMDb_Movie_Genres_chart.svg)

### 关键发现

- **类型总数**：IMDb 数据库共收录 **28 种**电影类型，覆盖了主流影视类型（动作、喜剧、剧情、恐怖、科幻等）以及细分类型（如 Film-Noir、Game-Show、Reality-TV、Talk-Show 等）。
- **类型多样性**：类型体系既包含传统电影类型（Drama、Comedy、Action），也包含电视节目类型（Game-Show、Reality-TV、Talk-Show、News），说明该数据库同时覆盖电影与电视内容。
- **执行策略**：采用**策略 B（快速通道）**，该查询为单表简单查询，无需复杂聚合，执行高效。

## 5. 附录

### 执行流程追溯

- Phase 1 (Knowledge Loader): 加载 genres 相关业务知识 → 1 次 LLM 调用
- Step 2 (Schema Linking): 使用 `genres` 表 → 1 次 LLM 调用
- Step 3 (Subproblem): 识别到单表查询子句 → 1 次 LLM 调用
- Step 4 (Query Plan): 简单查询计划 → 1 次 LLM 调用
- Step 5 (SQL Generation): 生成 + 后处理 → 1 次 LLM 调用
- Step 6 (Execute): 成功（返回 28 行）

### 统计

- 总 LLM 调用次数: 6
- 是否进入纠错循环: 否
