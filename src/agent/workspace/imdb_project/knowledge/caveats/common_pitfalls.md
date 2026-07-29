# IMDb 查询常见陷阱与注意事项

## 一、Schema 理解陷阱

### 1.1 模型名与表名
- 模型名 = 数据库表名（无后缀）
- 例如：数据库表 `titles` → 模型名 `titles`
- **正确示例**：`FROM titles`

### 1.2 主键类型
- `titles.id` 和 `names.id` 是 VARCHAR 类型（非自增数字）
- 格式：`tt1375666`（作品）、`nm0000102`（人物）
- **不要对 id 做数学运算或自增假设**

### 1.3 genre_id 是字符串
- `genres.id` 存的是字符串：`'action'`, `'comedy'`, `'drama'` 等
- **不是数字 ID**，不要用整数比较

### 1.4 category_id 区分性别
- 演员分 `actor` 和 `actress` 两个类别
- 查询"演员"时必须同时包含两者：`category_id IN ('actor', 'actress')`

## 二、数据质量陷阱

### 2.1 NULL 值泛滥
以下字段经常为 NULL，查询时必须处理：
| 字段 | NULL 场景 | 建议处理 |
|------|----------|---------|
| start_year | 未记录年份 | `WHERE start_year IS NOT NULL` |
| runtime_minutes | 剧集通常为 NULL | `WHERE runtime_minutes IS NOT NULL` 或 `COALESCE` |
| end_year | 电影和未完结剧集 | 仅电视剧系列有值 |
| death_year | 健在人物 | NULL = 健在 |
| region/language | 别名未标注地区 | `WHERE region IS NOT NULL` |

### 2.2 评分可信度
- **低投票数影片评分不可靠**
- 排名查询必须加 `num_votes > 10000` 过滤
- 示例：某部只有 5 票的影片评分 10.0，不代表它比《肖申克的救赎》好

## 三、业务语义陷阱

### 3.1 "电影" vs "作品"
- `title_type = 'movie'` 才是电影
- 不要用 `titles` 表代表"电影"，它包含电视剧、短片、游戏等

### 3.2 "演员"包含 actor 和 actress
- **常见错误**：`WHERE category_id = 'actor'`（漏掉了女演员）
- **正确写法**：`WHERE category_id IN ('actor', 'actress')`

### 3.3 "导演"就是 director
- 导演的 category_id = 'director'
- 不要用 job_id 来查导演（job 是自由格式，不标准）

### 3.4 电视剧 vs 剧集
- `tvSeries` = 电视剧系列（如《老友记》整体）
- `tvEpisode` = 单个剧集（如《老友记》S01E01）
- 通过 `episodes` 表关联：`episodes.parent_id = series.id` 且 `episodes.id = episode.id`

### 3.5 别名（titleakas）的 region 含义
- `region = 'CN'` = 中文地区发行名
- `region = 'US'` = 美国地区名
- `is_original_title = 1` = 原始标题
- 同一部电影在不同地区可能有完全不同的别名

## 四、SQL 写法陷阱

### 4.1 多表 JOIN 必须指定别名
```sql
-- 正确
SELECT t.primary_title, n.primary_name
FROM titles t
JOIN principals p ON t.id = p.title_id
JOIN names n ON p.name_id = n.id

-- 错误（字段名冲突）
SELECT primary_title, primary_name
FROM titles
JOIN principals ON titles.id = principals.title_id
```

### 4.2 聚合查询的 NULL 处理
```sql
-- 正确：处理 NULL
AVG(COALESCE(t.average_rating, 0))
COUNT(DISTINCT CASE WHEN p.category_id IN ('actor','actress') THEN p.name_id END)

-- 错误：NULL 参与计算
AVG(t.average_rating)  -- 如果全部为 NULL 返回 NULL
```

### 4.3 百分比计算防除零
```sql
-- 正确
COUNT(*) * 100.0 / NULLIF(total, 0)

-- 错误（除零崩溃）
COUNT(*) * 100 / total
```

### 4.4 窗口函数使用
```sql
-- 正确：指定窗口范围
AVG(t.average_rating) OVER (PARTITION BY g.genre_id)

-- 正确：求整体平均
AVG(t.average_rating) OVER ()
```

## 五、性能陷阱

### 5.2 避免 SELECT *
```sql
-- 不推荐
SELECT * FROM titles JOIN principals ...

-- 推荐
SELECT t.id, t.primary_title, p.category_id ...
```

### 5.3 善用子查询代替多层 JOIN
当需要先聚合再关联时，子查询或 CTE 更清晰：
```sql
WITH actor_stats AS (
  SELECT name_id, COUNT(*) AS movie_count, AVG(average_rating) AS avg_rating
  FROM principals p JOIN titles t ON p.title_id = t.id
  WHERE p.category_id IN ('actor','actress') AND t.title_type = 'movie'
  GROUP BY name_id
  HAVING COUNT(*) >= 10
)
SELECT n.primary_name, s.movie_count, s.avg_rating
FROM names n JOIN actor_stats s ON n.id = s.name_id
ORDER BY s.avg_rating DESC
```

## 六、常见误解纠正

| 误解 | 事实 |
|------|------|
| "titles 表就是电影表" | 包含 9 种作品类型，需过滤 title_type='movie' |
| "actor 就是所有演员" | 需同时查 actor 和 actress |
| "评分高就是好电影" | 需结合投票数判断可信度 |
| "电视剧的 runtime 是片长" | 剧集 runtime 通常为 NULL |
| "genre_id 是数字" | 是字符串如 'action', 'comedy' |
| "names.id 是自增数字" | 是 'nm0000102' 格式的字符串 |
| "end_year 对所有作品都有意义" | 仅电视剧系列使用 |
| "titleakas 是翻译名" | 包含地区别名、工作标题、DVD 标题等多种类型 |
