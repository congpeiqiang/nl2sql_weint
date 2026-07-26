# IMDb 指标定义手册

> 本文档定义所有可在 NL2SQL 查询中使用的业务指标。
> 每个指标包含：定义、SQL 公式、使用场景、注意事项。

---

## 一、评分质量类指标

### 1.1 基础评分指标

| 指标名 | 公式 | 场景 |
|--------|------|------|
| 平均评分 | `AVG(t.average_rating)` | 聚合统计 |
| 评分中位数 | 需用 PERCENTILE_CONT 或行号法 | 分布分析 |
| 评分标准差 | `STDDEV(t.average_rating)` | 评分离散度 |
| 最高评分 | `MAX(t.average_rating)` | 找最佳 |
| 最低评分 | `MIN(t.average_rating)` | 找最差 |

### 1.2 质量得分（Quality Score）
```
Quality Score = average_rating × LOG10(num_votes + 1) × (1 + 0.1 × (runtime_minutes / 120 - 1))
```
- **用途**：综合评分、投票数、片长三个维度的影片质量评估
- **说明**：LOG10 对投票数做非线性缩放，片长偏离 120 分钟会轻微扣分
- **SQL 示例**：
```sql
SELECT primary_title, average_rating, num_votes, runtime_minutes,
  ROUND(average_rating * LOG10(num_votes + 1) * (1 + 0.1 * (runtime_minutes / 120.0 - 1)), 4) AS quality_score
  FROM titles
  WHERE title_type = 'movie' AND num_votes > 1000 AND average_rating IS NOT NULL AND runtime_minutes IS NOT NULL
ORDER BY quality_score DESC
LIMIT 20
```

### 1.3 贝叶斯加权评分（Bayesian Adjusted Rating）
```
Bayesian Rating = (num_votes × average_rating + C × global_avg) / (num_votes + C)
其中 C = 1000（最小可信投票数）, global_avg = 所有电影平均评分
```
- **用途**：解决低投票数影片评分虚高/虚低问题
- **SQL 示例**：
```sql
SELECT id, primary_title, average_rating, num_votes,
  (num_votes * average_rating + 1000 * (SELECT AVG(average_rating) FROM titles WHERE title_type = 'movie' AND num_votes > 1000))
  / (num_votes + 1000) AS bayesian_rating
FROM titles
WHERE title_type = 'movie' AND average_rating IS NOT NULL
ORDER BY bayesian_rating DESC
LIMIT 20
```

### 1.4 投票加权评分（Vote-Weighted Rating）
```
Vote-Weighted = SUM(average_rating × num_votes) / SUM(num_votes)
```
- **用途**：计算一组影片的整体评价（如某导演全部作品）
- **场景**：导演作品集、类型整体评价、年代整体评价

---

## 二、人物生产力指标

### 2.1 基础产量指标

| 指标名 | 公式 | 场景 |
|--------|------|------|
| 作品总数 | `COUNT(DISTINCT p.title_id)` | 衡量产出量 |
| 角色多样性 | `COUNT(DISTINCT p.category_id)` | 多面手程度 |
| 合作导演数 | `COUNT(DISTINCT ... WHERE category_id='director')` | 人脉广度 |
| 合作演员数 | `COUNT(DISTINCT ... WHERE category_id IN ('actor','actress'))` | 合作网络 |

### 2.2 职业生涯跨度
```
Career Span = MAX(t.start_year) - MIN(t.start_year)
```
- **用途**：衡量从业时长
- **注意**：仅统计有 start_year 的作品

### 2.3 活跃密度（Productivity Density）
```
Active Density = COUNT(DISTINCT p.title_id) / (MAX(t.start_year) - MIN(t.start_year) + 1)
```
- **用途**：年均产出作品数，衡量创作效率
- **场景**：高产型 vs 精耕型创作者分类

### 2.4 Star Power Index（明星号召力指数）
```
Star Power Index = AVG(导演作品评分) × LOG(SUM(导演作品投票数)) × (导演作品数 / 职业生涯跨度)
```
- **用途**：综合衡量导演的号召力和影响力
- **SQL 示例**：
```sql
SELECT n.primary_name,
  COUNT(DISTINCT p.title_id) AS movie_count,
  ROUND(AVG(t.average_rating), 2) AS avg_rating,
  ROUND(LOG(SUM(t.num_votes)), 2) AS log_votes,
  ROUND(AVG(t.average_rating) * LOG(SUM(t.num_votes)) * COUNT(DISTINCT p.title_id) / NULLIF(MAX(t.start_year) - MIN(t.start_year), 0), 4) AS star_power
FROM names n
JOIN principals p ON n.id = p.name_id AND p.category_id = 'director'
JOIN titles t ON p.title_id = t.id
WHERE t.title_type = 'movie' AND t.start_year IS NOT NULL AND t.num_votes > 1000
GROUP BY n.id, n.primary_name
HAVING COUNT(DISTINCT p.title_id) >= 5
ORDER BY star_power DESC
```

---

## 三、类型分析指标

### 3.1 类型 ROI（投资回报率）
```
ROI = (类型平均评分 - 整体平均评分) / 整体平均评分 × (类型平均投票数 / 整体平均投票数)
```
- **用途**：衡量各类型影片的相对表现
- **说明**：> 0 表示优于整体水平，< 0 表示低于整体

### 3.2 类型市场份额
```
Market Share = 该年代某类型电影数 / 该年代电影总数 × 100%
```
- **用途**：分析类型流行度的时间变化
- **场景**：某类型是否在特定年代更受欢迎

### 3.3 类型多样性指数
```
Diversity = COUNT(DISTINCT genre_id) per title
```
- **用途**：衡量影片的类型融合程度

---

## 四、剧集分析指标

### 4.1 评分趋势（Rating Trend）
```
Trend = CORR(season_number, average_rating)
```
- **用途**：判断剧集质量走向（正=越来越好，负=越来越差）

### 4.2 评分衰退率
```
Decline Rate = (S1_avg_rating - S_last_avg_rating) / S1_avg_rating
```
- **用途**：衡量剧集质量下滑幅度
- **阈值**：> 30% 为严重衰退

### 4.3 最佳完结信号
```
Signal = 某季评分首次 < 该剧平均评分的 80%
```
- **用途**：辅助判断剧集应在何时完结

---

## 五、商业决策指标

### 5.1 电影投资评分卡
```
Investment Score =
  0.35 × (average_rating / 10) +
  0.25 × (LOG(num_votes + 1) / LOG(MAX_VOTES + 1)) +
  0.20 × (1 - ABS(runtime_minutes - 120) / 120) +
  0.20 × (CASE WHEN is_adult = 0 THEN 1 ELSE 0.3 END)
```
- **用途**：多维度综合评分辅助投资决策
- **权重说明**：评分 35%、热度 25%、片长合理性 20%、内容普适性 20%

### 5.2 阵容强度指数
```
Cast Strength = (个人平均评分 > 7.0 的演员数) / 总参演演员数
```
- **用途**：衡量电影演员阵容的质量强度
- **分档**：强 >= 0.6, 中 0.3-0.6, 弱 < 0.3

---

## 六、异常检测指标

### 6.1 评分偏差度
```
Rating Deviation = ABS(average_rating - bayesian_rating)
```
- **用途**：检测评分可能不可信的影片
- **阈值**：偏差 > 1.0 需关注

### 6.2 宝藏片指数
```
Hidden Gem Score = average_rating / LOG10(num_votes + 1)
```
- **用途**：高评分但低关注度的影片
- **阈值**：rating >= 8.5 AND votes < 5000

### 6.3 热度异常指数
```
Popularity Anomaly = num_votes / AVG(num_votes) OVER (PARTITION BY genre_id)
```
- **用途**：检测某类型中异常热门的影片
