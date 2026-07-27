# IMDb 评分最高电影 Top 5 查询报告

## 查询概述
查询 IMDb 数据库中 `average_rating` 最高的 5 部电影（`title_type='movie'`），以获取评分排名前列的电影信息。

## 数据结果

| 排名 | 片名 | 上映年份 | 评分 |
|:---:|:---|:---:|:---:|
| 1 | Riding for Jesus | 2011 | 10.0 |
| 2 | And God Created Trans (I Bog stvori Trans) | 2025 | 10.0 |
| 3 | La Reina, el Buda y Buñuel | 2026 | 10.0 |
| 4 | Copps | 2026 | 10.0 |
| 5 | Bienvenue en Islande | 2026 | 10.0 |

## 关键发现
- 5 部电影均获得 IMDb 满分 10.0
- 投票数可能较少，评分可信度与投票数相关
- 如需更可靠排行可增加 `num_votes >= 1000` 门槛

## 执行信息
- **查询引擎**：NL2SQL 子智能体
- **任务 ID**：019fa148-b01b-7e62-873d-eda64976d7b6
- **执行耗时**：4 分 33 秒
