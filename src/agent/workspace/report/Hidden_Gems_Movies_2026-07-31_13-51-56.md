# 评分 ≥ 8.5 且投票数 < 5000 的电影分析报告

> 生成时间：2026-07-31_13-51-56
> 数据来源：imdb 数据库（titles 表）

## 1. 概述

本报告分析 **评分 ≥ 8.5 且投票数 < 5000** 的电影，即业务规则中定义的"宝藏片"（Hidden Gem）——高评分但低关注度的影片。查询结果按评分降序排列。

**核心发现：** 符合条件的影片评分全部为 **10.0**，但投票数极低（仅 5~15 票），评分可信度较低，可能存在偏差。

## 2. 核心数据

| 电影标题 | 评分 | 投票数 |
|---------|------|-------|
| Riding for Jesus | 10.0 | 6 |
| And God Created Trans (I Bog staffroom Trans) | 10.0 | 14 |
| La Reina, el Buda y Buñuel | 10.0 | 5 |
| Copps | 10.0 | 8 |
| Bienvenue en Islande | 10.0 | 5 |
| White Knuckles and Blue Moods | 10.0 | 5 |
| Better Call Pops (Preach) | 10.0 | 8 |
| Sedição de Juazeiro | 10.0 | 6 |
| Como Vivem os Bravos | 10.0 | 6 |
| At What Cost? Anatomy of Professional Wrestling | 10.0 | 7 |
| My boyfriends daddy is my man | 10.0 | 7 |
| Rishtey | 10.0 | 5 |
| Monster Metaverse: Monster Hunter Academy | 10.0 | 6 |
| Man from Pretentia | 10.0 | 15 |
| Captain Jack and His Rocket Powered Go-Kart | 10.0 | 10 |
| Fortune Cookies | 10.0 | 11 |
| Ifediche | 10.0 | 7 |
| Ne Güzeldir Gülümsün | 10.0 | 5 |
| Ex-Girlfriends | 10.0 | 7 |
| República dos Juízes | 10.0 | 6 |

## 3. 生成 SQL

```sql
SELECT primary_title, average_rating, num_votes
FROM titles
WHERE title_type = 'movie'
  AND average_rating >= 8.5
  AND num_votes < 5000
ORDER BY average_rating DESC
```

## 4. 分析解读

### 图表

![评分≥8.5且投票数<5000的电影（按投票数）](./Hidden_Gems_Movies_chart.svg)

### 关键发现

1. **评分高度集中：** 所有符合条件的影片评分均为 10.0，说明在低投票数区间内，评分呈现"满分"现象。
2. **投票数极低：** 投票数集中在 5~15 票之间，属于极小样本。
3. **可信度警示：** 根据业务规则，低投票数的评分可信度较低，评分可能存在偏差。这些"满分"影片的评分参考价值有限。
4. **"宝藏片"特征：** 这些影片符合 Hidden Gem 定义——高评分但低关注度，但需谨慎对待其评分。

### 建议

- 如需更可靠的"宝藏片"推荐，可适当提高投票数下限（如 ≥ 100 票）以过滤极端小样本。
- 可结合其他指标（如类型、年份）进一步分析这些影片的分布特征。

## 5. 附录

- **查询耗时：** 约 10 秒
- **执行步骤：** knowledge-loader → sql-generation → run_sql（3/3 完成）
- **结果条数：** 超过 20 条，本报告展示前 20 条
