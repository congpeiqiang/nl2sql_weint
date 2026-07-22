# IMDB NL2SQL 深度测试方案

## 1、项目背景

基于 WrenAI 框架的 IMDB 数据集 NL2SQL 验证项目。数据库包含 17 张表，涵盖作品（titles）、人物（names）、参与者（principals）、剧集（episodes）、类型（genres）、别名（titleakas）等核心 IMDb 数据。

**数据规模：**
- titles: ~1000万+ 条
- names: ~1200万+ 条
- principals: ~5000万+ 条

---

## 2、基础设施设计（需预先配置）

### 2.1 Knowledge 知识库设计

#### `knowledge/knowledge.yml`

```yaml
version: 1
description: IMDb 知识库 — 业务规则、查询示例、指标定义
```

#### caveats/common_pitfalls.md

~~~markdown
# IMDb 查询常见陷阱与注意事项

## 一、Schema 理解陷阱

### 1.1 表名带 `_t` 后缀
- 模型名 = 数据库表名 + `_t` 后缀
- 例如：数据库表 `titles` → 模型名 `titles_t`
- **错误示例**：`FROM titles` → **正确**：`FROM titles_t`

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

### 2.3 数据规模
- titles: ~1000 万条
- names: ~1200 万条
- principals: ~5000 万条
- **避免无过滤条件的全表扫描**，必须加 WHERE 条件

## 三、业务语义陷阱

### 3.1 "电影" vs "作品"
- `title_type = 'movie'` 才是电影
- 不要用 `titles_t` 表代表"电影"，它包含电视剧、短片、游戏等

### 3.2 "演员"包含 actor 和 actress
- **常见错误**：`WHERE category_id = 'actor'`（漏掉了女演员）
- **正确写法**：`WHERE category_id IN ('actor', 'actress')`

### 3.3 "导演"就是 director
- 导演的 category_id = 'director'
- 不要用 job_id 来查导演（job 是自由格式，不标准）

### 3.4 电视剧 vs 剧集
- `tvSeries` = 电视剧系列（如《老友记》整体）
- `tvEpisode` = 单个剧集（如《老友记》S01E01）
- 通过 `episodes_t` 表关联：`episodes_t.parent_id = series.id` 且 `episodes_t.id = episode.id`

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
FROM titles_t t
JOIN principals_t p ON t.id = p.title_id
JOIN names_t n ON p.name_id = n.id

-- 错误（字段名冲突）
SELECT primary_title, primary_name
FROM titles_t
JOIN principals_t ON titles_t.id = principals_t.title_id
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

### 5.1 避免无 LIMIT 的排序
```sql
-- 不推荐（全表排序）
SELECT * FROM titles_t ORDER BY average_rating DESC

-- 推荐（加 LIMIT）
SELECT * FROM titles_t WHERE num_votes > 10000 ORDER BY average_rating DESC LIMIT 100
```

### 5.2 避免 SELECT *
```sql
-- 不推荐
SELECT * FROM titles_t JOIN principals_t ...

-- 推荐
SELECT t.id, t.primary_title, p.category_id ...
```

### 5.3 善用子查询代替多层 JOIN
当需要先聚合再关联时，子查询或 CTE 更清晰：
```sql
WITH actor_stats AS (
  SELECT name_id, COUNT(*) AS movie_count, AVG(average_rating) AS avg_rating
  FROM principals_t p JOIN titles_t t ON p.title_id = t.id
  WHERE p.category_id IN ('actor','actress') AND t.title_type = 'movie'
  GROUP BY name_id
  HAVING COUNT(*) >= 10
)
SELECT n.primary_name, s.movie_count, s.avg_rating
FROM names_t n JOIN actor_stats s ON n.id = s.name_id
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

~~~

#### glossary/imdb_glossary.md

~~~markdown
# IMDb 查询常见陷阱与注意事项

## 一、Schema 理解陷阱

### 1.1 表名带 `_t` 后缀
- 模型名 = 数据库表名 + `_t` 后缀
- 例如：数据库表 `titles` → 模型名 `titles_t`
- **错误示例**：`FROM titles` → **正确**：`FROM titles_t`

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

### 2.3 数据规模
- titles: ~1000 万条
- names: ~1200 万条
- principals: ~5000 万条
- **避免无过滤条件的全表扫描**，必须加 WHERE 条件

## 三、业务语义陷阱

### 3.1 "电影" vs "作品"
- `title_type = 'movie'` 才是电影
- 不要用 `titles_t` 表代表"电影"，它包含电视剧、短片、游戏等

### 3.2 "演员"包含 actor 和 actress
- **常见错误**：`WHERE category_id = 'actor'`（漏掉了女演员）
- **正确写法**：`WHERE category_id IN ('actor', 'actress')`

### 3.3 "导演"就是 director
- 导演的 category_id = 'director'
- 不要用 job_id 来查导演（job 是自由格式，不标准）

### 3.4 电视剧 vs 剧集
- `tvSeries` = 电视剧系列（如《老友记》整体）
- `tvEpisode` = 单个剧集（如《老友记》S01E01）
- 通过 `episodes_t` 表关联：`episodes_t.parent_id = series.id` 且 `episodes_t.id = episode.id`

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
FROM titles_t t
JOIN principals_t p ON t.id = p.title_id
JOIN names_t n ON p.name_id = n.id

-- 错误（字段名冲突）
SELECT primary_title, primary_name
FROM titles_t
JOIN principals_t ON titles_t.id = principals_t.title_id
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

### 5.1 避免无 LIMIT 的排序
```sql
-- 不推荐（全表排序）
SELECT * FROM titles_t ORDER BY average_rating DESC

-- 推荐（加 LIMIT）
SELECT * FROM titles_t WHERE num_votes > 10000 ORDER BY average_rating DESC LIMIT 100
```

### 5.2 避免 SELECT *
```sql
-- 不推荐
SELECT * FROM titles_t JOIN principals_t ...

-- 推荐
SELECT t.id, t.primary_title, p.category_id ...
```

### 5.3 善用子查询代替多层 JOIN
当需要先聚合再关联时，子查询或 CTE 更清晰：
```sql
WITH actor_stats AS (
  SELECT name_id, COUNT(*) AS movie_count, AVG(average_rating) AS avg_rating
  FROM principals_t p JOIN titles_t t ON p.title_id = t.id
  WHERE p.category_id IN ('actor','actress') AND t.title_type = 'movie'
  GROUP BY name_id
  HAVING COUNT(*) >= 10
)
SELECT n.primary_name, s.movie_count, s.avg_rating
FROM names_t n JOIN actor_stats s ON n.id = s.name_id
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
~~~

#### metrics/imdb_metrics.md

~~~markdown
# IMDb 查询常见陷阱与注意事项

## 一、Schema 理解陷阱

### 1.1 表名带 `_t` 后缀
- 模型名 = 数据库表名 + `_t` 后缀
- 例如：数据库表 `titles` → 模型名 `titles_t`
- **错误示例**：`FROM titles` → **正确**：`FROM titles_t`

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

### 2.3 数据规模
- titles: ~1000 万条
- names: ~1200 万条
- principals: ~5000 万条
- **避免无过滤条件的全表扫描**，必须加 WHERE 条件

## 三、业务语义陷阱

### 3.1 "电影" vs "作品"
- `title_type = 'movie'` 才是电影
- 不要用 `titles_t` 表代表"电影"，它包含电视剧、短片、游戏等

### 3.2 "演员"包含 actor 和 actress
- **常见错误**：`WHERE category_id = 'actor'`（漏掉了女演员）
- **正确写法**：`WHERE category_id IN ('actor', 'actress')`

### 3.3 "导演"就是 director
- 导演的 category_id = 'director'
- 不要用 job_id 来查导演（job 是自由格式，不标准）

### 3.4 电视剧 vs 剧集
- `tvSeries` = 电视剧系列（如《老友记》整体）
- `tvEpisode` = 单个剧集（如《老友记》S01E01）
- 通过 `episodes_t` 表关联：`episodes_t.parent_id = series.id` 且 `episodes_t.id = episode.id`

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
FROM titles_t t
JOIN principals_t p ON t.id = p.title_id
JOIN names_t n ON p.name_id = n.id

-- 错误（字段名冲突）
SELECT primary_title, primary_name
FROM titles_t
JOIN principals_t ON titles_t.id = principals_t.title_id
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

### 5.1 避免无 LIMIT 的排序
```sql
-- 不推荐（全表排序）
SELECT * FROM titles_t ORDER BY average_rating DESC

-- 推荐（加 LIMIT）
SELECT * FROM titles_t WHERE num_votes > 10000 ORDER BY average_rating DESC LIMIT 100
```

### 5.2 避免 SELECT *
```sql
-- 不推荐
SELECT * FROM titles_t JOIN principals_t ...

-- 推荐
SELECT t.id, t.primary_title, p.category_id ...
```

### 5.3 善用子查询代替多层 JOIN
当需要先聚合再关联时，子查询或 CTE 更清晰：
```sql
WITH actor_stats AS (
  SELECT name_id, COUNT(*) AS movie_count, AVG(average_rating) AS avg_rating
  FROM principals_t p JOIN titles_t t ON p.title_id = t.id
  WHERE p.category_id IN ('actor','actress') AND t.title_type = 'movie'
  GROUP BY name_id
  HAVING COUNT(*) >= 10
)
SELECT n.primary_name, s.movie_count, s.avg_rating
FROM names_t n JOIN actor_stats s ON n.id = s.name_id
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

~~~

#### rules/general.md

~~~markdown
# IMDb 通用业务规则

## 一、作品类型语义

| title_type | 中文含义 | 说明 |
|-----------|---------|------|
| movie | 电影 | 长片电影，通常 runtime >= 40 分钟 |
| short | 短片 | runtime < 40 分钟 |
| tvSeries | 电视剧系列 | 多季多集的电视剧 |
| tvEpisode | 剧集 | 电视剧的单个剧集，通过 episodes 表关联父系列 |
| tvMovie | 电视电影 | 为电视制作的电影 |
| tvMiniSeries | 迷你剧 | 有限集数的电视剧 |
| tvSpecial | 电视特辑 | 特别节目 |
| video | 录像带/数字发行 | 非院线发行的视频 |
| videoGame | 电子游戏 | 基于影视作品的游戏 |

## 二、演员定义规则

- **查询"演员"时**：必须同时包含 `category_id IN ('actor', 'actress')`
  - IMDb 按性别区分，但业务上"演员"应包含两者
- **查询"主要演员"时**：额外增加 `p.ordering <= 3`
  - ordering=1 为主演，值越小越重要
- **查询"全体演职人员"时**：使用 `principals` 表，不限制 category_id

## 三、评分可信度规则

| 投票数阈值 | 可信度 | 适用场景 |
|-----------|--------|---------|
| < 1000 | 低 | 评分偏差大，不建议用于排名 |
| >= 1000 | 基本可信 | 一般分析可用 |
| >= 10000 | 较可信 | 推荐用于排行榜 |
| >= 50000 | 高可信 | 经典影片级别 |
| >= 200000 | 极高 | 现象级影片 |

**推荐做法：** 排名类查询应加 `num_votes > 10000` 过滤

## 四、NULL 值处理

- `start_year`：可能为 NULL，查询时建议 `WHERE start_year IS NOT NULL`
- `end_year`：电视剧系列专用，电影通常为 NULL
- `runtime_minutes`：电视剧集通常为 NULL，电影一般有值
- `death_year`：NULL 表示人物仍健在
- `region` / `language`（titleakas 表）：可为 NULL

## 五、年代划分

- 年代（decade）计算公式：`FLOOR(start_year / 10) * 10`
- 示例：1994 → 1990, 2005 → 2000, 2023 → 2020

## 六、片长分类（针对 movie 类型）

| 片长范围 | 分类 |
|---------|------|
| < 40 min | 短片 |
| 40-90 min | 标准片 |
| 90-150 min | 长片 |
| > 150 min | 超长片 |

## 七、剧集状态判断

- `end_year IS NULL` → 仍在播
- `end_year IS NOT NULL AND end_year >= YEAR(CURDATE()) - 2` → 近期完结
- `end_year IS NOT NULL AND end_year < YEAR(CURDATE()) - 2` → 已完结

## 八、多表 JOIN 路径

```
# 核心路径：作品 ↔ 参与者 ↔ 人物
titles_t ←→ principals_t ←→ names_t
    ↓                           ↓
titles_genres_t          names_primaryprofessions_t
    ↓                           ↓
genres_t                 professions_t

# 剧集路径
titles_t (series) ←→ episodes_t ←→ titles_t (episode)

# 别名路径
titles_t ←→ titleakas_t

# 角色路径
principals_t ←→ principals_characters_t
```

## 九、常用过滤模式

```sql
-- 只查电影
WHERE t.title_type = 'movie'

-- 只查电视剧系列
WHERE t.title_type = 'tvSeries'

-- 查电影且排除短片
WHERE t.title_type = 'movie' AND (t.runtime_minutes >= 40 OR t.runtime_minutes IS NULL)

-- 查近 N 年
WHERE t.start_year >= YEAR(CURDATE()) - N

-- 查高可信评分
WHERE t.num_votes > 10000 AND t.average_rating IS NOT NULL
```
~~~

#### sql/cross_over_artists.md

~~~markdown
# 演而优则导 — 跨界者表现分析

## 查询说明
找出同时是演员和导演的人，对比其作为演员 vs 导演的作品评分差异。

```sql
WITH actor_works AS (
  SELECT p.name_id, AVG(t.average_rating) AS actor_avg_rating, COUNT(*) AS actor_movie_count
  FROM principals_t p
  JOIN titles_t t ON p.title_id = t.id
  WHERE p.category_id IN ('actor', 'actress') AND t.title_type = 'movie' AND t.average_rating IS NOT NULL
  GROUP BY p.name_id
  HAVING COUNT(*) >= 3
),
director_works AS (
  SELECT p.name_id, AVG(t.average_rating) AS director_avg_rating, COUNT(*) AS director_movie_count
  FROM principals_t p
  JOIN titles_t t ON p.title_id = t.id
  WHERE p.category_id = 'director' AND t.title_type = 'movie' AND t.average_rating IS NOT NULL
  GROUP BY p.name_id
  HAVING COUNT(*) >= 2
)
SELECT
  n.primary_name,
  a.actor_movie_count,
  ROUND(a.actor_avg_rating, 2) AS actor_avg_rating,
  d.director_movie_count,
  ROUND(d.director_avg_rating, 2) AS director_avg_rating,
  ROUND(d.director_avg_rating - a.actor_avg_rating, 2) AS rating_difference
FROM actor_works a
JOIN director_works d ON a.name_id = d.name_id
JOIN names_t n ON a.name_id = n.id
ORDER BY rating_difference DESC
LIMIT 20
```
~~~

### 2.2 Views 视图设计

**文件：** `views/` 目录下

#### view_1: `movie_summary_view.yml` — 电影综合摘要视图

```yaml
name: movie_summary_view
description: "电影综合摘要视图，聚合每部电影的核心指标"
model_reference:
  - titles_t
  - titles_genres_t
  - genres_t
  - principals_t
  - names_t
query: |
  SELECT
    t.id,
    t.primary_title,
    t.start_year,
    t.runtime_minutes,
    t.average_rating,
    t.num_votes,
    g.display_name AS genre,
    COUNT(DISTINCT p.name_id) AS total_participants,
    COUNT(DISTINCT CASE WHEN p.category_id IN ('actor','actress') THEN p.name_id END) AS cast_count,
    COUNT(DISTINCT CASE WHEN p.category_id = 'director' THEN p.name_id END) AS director_count
  FROM titles_t t
  LEFT JOIN titles_genres_t tg ON t.id = tg.title_id
  LEFT JOIN genres_t g ON tg.genre_id = g.id
  LEFT JOIN principals_t p ON t.id = p.title_id
  WHERE t.title_type = 'movie'
  GROUP BY t.id, t.primary_title, t.start_year, t.runtime_minutes,
           t.average_rating, t.num_votes, g.display_name
```

#### view_2: `person_career_view.yml` — 人物职业生涯视图

```yaml
name: person_career_view
description: "人物职业生涯聚合视图"
model_reference:
  - names_t
  - principals_t
  - titles_t
query: |
  SELECT
    n.id AS person_id,
    n.primary_name,
    n.birth_year,
    n.death_year,
    COUNT(DISTINCT p.title_id) AS total_titles,
    COUNT(DISTINCT p.category_id) AS role_types,
    MIN(t.start_year) AS career_start,
    MAX(t.start_year) AS career_end,
    MAX(t.start_year) - MIN(t.start_year) AS career_span_years,
    ROUND(AVG(t.average_rating), 2) AS avg_rating_of_works,
    SUM(t.num_votes) AS total_votes_received
  FROM names_t n
  JOIN principals_t p ON n.id = p.name_id
  JOIN titles_t t ON p.title_id = t.id
  WHERE t.start_year IS NOT NULL
  GROUP BY n.id, n.primary_name, n.birth_year, n.death_year
```

#### view_3: `series_episode_view.yml` — 电视剧剧集视图

```yaml
name: series_episode_view
description: "电视剧系列及其剧集信息视图"
model_reference:
  - titles_t AS series
  - episodes_t
  - titles_t AS episode
query: |
  SELECT
    s.id AS series_id,
    s.primary_title AS series_title,
    s.start_year AS series_start_year,
    s.end_year AS series_end_year,
    e.id AS episode_id,
    ep.primary_title AS episode_title,
    e.season_number,
    e.episode_number,
    ep.start_year AS episode_year,
    ep.average_rating AS episode_rating,
    ep.runtime_minutes AS episode_runtime
  FROM titles_t s
  JOIN episodes_t e ON s.id = e.parent_id
  JOIN titles_t ep ON e.id = ep.id
  WHERE s.title_type = 'tvSeries'
```

#### view_4: `genre_stats_view.yml` — 类型统计视图

```yaml
name: genre_stats_view
description: "按类型聚合的统计指标视图"
model_reference:
  - genres_t
  - titles_genres_t
  - titles_t
query: |
  SELECT
    g.id AS genre_id,
    g.display_name AS genre_name,
    COUNT(DISTINCT tg.title_id) AS title_count,
    ROUND(AVG(t.average_rating), 2) AS avg_rating,
    ROUND(AVG(t.num_votes), 0) AS avg_votes,
    ROUND(AVG(t.runtime_minutes), 1) AS avg_runtime,
    MIN(t.start_year) AS first_appearance,
    MAX(t.start_year) AS last_appearance,
    SUM(CASE WHEN t.title_type = 'movie' THEN 1 ELSE 0 END) AS movie_count,
    SUM(CASE WHEN t.title_type = 'tvSeries' THEN 1 ELSE 0 END) AS series_count
  FROM genres_t g
  JOIN titles_genres_t tg ON g.id = tg.genre_id
  JOIN titles_t t ON tg.title_id = t.id
  GROUP BY g.id, g.display_name
```

### 2.3 Cubes 多维分析设计

**文件：** `cubes/` 目录下

#### cube_1: `movie_analytics_cube.yml` — 电影分析立方体

```yaml
name: movie_analytics_cube
description: "电影多维分析立方体 — 支持按年代、类型、评分区间等维度下钻"
measures:
  - name: movie_count
    sql: "COUNT(DISTINCT id)"
    type: count
    description: "电影数量"

  - name: avg_rating
    sql: "AVG(average_rating)"
    type: avg
    description: "平均评分"

  - name: total_votes
    sql: "SUM(num_votes)"
    type: sum
    description: "总投票数"

  - name: avg_runtime
    sql: "AVG(runtime_minutes)"
    type: avg
    description: "平均片长（分钟）"

  - name: high_rated_ratio
    sql: "SUM(CASE WHEN average_rating >= 8.0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*)"
    type: number
    description: "高分片占比（评分>=8.0）"

  - name: popularity_index
    sql: "AVG(num_votes) / NULLIF(AVG(AVG(num_votes)) OVER (), 0)"
    type: number
    description: "热度指数（相对于整体平均）"

  - name: rating_votes_weighted
    sql: "SUM(average_rating * num_votes) / NULLIF(SUM(num_votes), 0)"
    type: number
    description: "投票加权平均评分（更准确的整体评价）"

dimensions:
  - name: decade
    sql: "FLOOR(start_year / 10) * 10"
    type: int
    description: "年代（如 1990, 2000, 2010）"

  - name: rating_tier
    sql: "CASE WHEN average_rating >= 8.0 THEN '高分(8-10)' WHEN average_rating >= 6.0 THEN '中分(6-8)' WHEN average_rating >= 4.0 THEN '低分(4-6)' ELSE '极低(0-4)' END"
    type: string
    description: "评分等级"

  - name: runtime_tier
    sql: "CASE WHEN runtime_minutes < 40 THEN '短片' WHEN runtime_minutes < 90 THEN '标准' WHEN runtime_minutes < 150 THEN '长片' ELSE '超长片' END"
    type: string
    description: "片长分类"

  - name: vote_tier
    sql: "CASE WHEN num_votes >= 200000 THEN '极高热度' WHEN num_votes >= 50000 THEN '高热度' WHEN num_votes >= 10000 THEN '中等热度' WHEN num_votes >= 1000 THEN '低热度' ELSE '冷门' END"
    type: string
    description: "热度等级"

  - name: is_adult_content
    sql: "CASE WHEN is_adult = 1 THEN '成人内容' ELSE '普通内容' END"
    type: string
    description: "成人内容标识"
```

#### cube_2: `person_productivity_cube.yml` — 人物生产力立方体

```yaml
name: person_productivity_cube
description: "影视从业者生产力分析立方体"
measures:
  - name: total_titles
    sql: "COUNT(DISTINCT p.title_id)"
    type: count
    description: "参与作品总数"

  - name: role_diversity
    sql: "COUNT(DISTINCT p.category_id)"
    type: count
    description: "角色类型多样性（担任过多少种不同职位）"

  - name: career_span
    sql: "MAX(t.start_year) - MIN(t.start_year)"
    type: number
    description: "职业生涯跨度（年）"

  - name: avg_work_rating
    sql: "AVG(t.average_rating)"
    type: avg
    description: "参与作品的平均评分"

  - name: productivity_density
    sql: "COUNT(DISTINCT p.title_id) * 1.0 / NULLIF(MAX(t.start_year) - MIN(t.start_year) + 1, 0)"
    type: number
    description: "生产力密度（年均产出作品数）"

  - name: total_votes_received
    sql: "SUM(t.num_votes)"
    type: sum
    description: "参与作品的总投票数"

dimensions:
  - name: profession
    sql: "pr.display_name"
    type: string
    description: "主要职业（从 names_primaryprofessions 关联）"

  - name: is_alive
    sql: "CASE WHEN n.death_year IS NULL THEN '健在' ELSE '已故' END"
    type: string
    description: "是否健在"

  - name: birth_decade
    sql: "FLOOR(n.birth_year / 10) * 10"
    type: int
    description: "出生年代"

  - name: alive_status
    sql: "CASE WHEN n.death_year IS NULL THEN 'living' ELSE 'deceased' END"
    type: string
```

#### cube_3: `series_health_cube.yml` — 剧集健康度立方体

```yaml
name: series_health_cube
description: "电视剧系列健康度分析立方体"
measures:
  - name: total_seasons
    sql: "COUNT(DISTINCT e.season_number)"
    type: count
    description: "总季数"

  - name: total_episodes
    sql: "COUNT(DISTINCT e.id)"
    type: count
    description: "总集数"

  - name: avg_episode_rating
    sql: "AVG(ep.average_rating)"
    type: avg
    description: "剧集平均评分"

  - name: rating_trend
    sql: "CORR(e.season_number, ep.average_rating)"
    type: number
    description: "评分趋势（正=越来越好，负=越来越差）"

  - name: episode_per_season
    sql: "COUNT(DISTINCT e.id) * 1.0 / NULLIF(COUNT(DISTINCT e.season_number), 0)"
    type: number
    description: "每季平均集数"

  - name: series_longevity
    sql: "MAX(ep.start_year) - MIN(ep.start_year)"
    type: number
    description: "播出跨度（年）"

dimensions:
  - name: series_status
    sql: "CASE WHEN s.end_year IS NULL THEN '在播' ELSE '已完结' END"
    type: string
    description: "系列状态"

  - name: season_number
    sql: "e.season_number"
    type: int
    description: "季号"
```

### 2.4 Relationships 补充设计

**当前 relationships.yml 存在的问题：** 缺少核心表之间的关键关联。需要补充：

```yaml
# ── 需要补充的核心关系 ──

# 1. principals → titles（核心：参与者-作品关联）
- name: principals_t_titles_t
  models:
    - principals_t
    - titles_t
  join_type: MANY_TO_ONE
  condition: principals_t.title_id = titles_t.id

# 2. principals → names（核心：参与者-人物关联）
- name: principals_t_names_t
  models:
    - principals_t
    - names_t
  join_type: MANY_TO_ONE
  condition: principals_t.name_id = names_t.id

# 3. principals → categories（参与者-职位类别）
- name: principals_t_categories_t
  models:
    - principals_t
    - categories_t
  join_type: MANY_TO_ONE
  condition: principals_t.category_id = categories_t.code

# 4. principals → jobs（参与者-具体职位）
- name: principals_t_jobs_t
  models:
    - principals_t
    - jobs_t
  join_type: MANY_TO_ONE
  condition: principals_t.job_id = jobs_t.code

# 5. episodes → titles (parent)（剧集-所属系列）
- name: episodes_t_titles_t_parent
  models:
    - episodes_t
    - titles_t
  join_type: MANY_TO_ONE
  condition: episodes_t.parent_id = titles_t.id

# 6. episodes → titles (episode)（剧集-自身标题）
- name: episodes_t_titles_t_episode
  models:
    - episodes_t
    - titles_t
  join_type: MANY_TO_ONE
  condition: episodes_t.id = titles_t.id

# 7. titles_genres → titles（作品-类型关联）
- name: titles_genres_t_titles_t
  models:
    - titles_genres_t
    - titles_t
  join_type: MANY_TO_ONE
  condition: titles_genres_t.title_id = titles_t.id

# 8. titles_genres → genres（类型-类型名关联）
- name: titles_genres_t_genres_t
  models:
    - titles_genres_t
    - genres_t
  join_type: MANY_TO_ONE
  condition: titles_genres_t.genre_id = genres_t.id

# 9. names_knownfortitles → titles（人物代表作）
- name: names_knownfortitles_t_titles_t
  models:
    - names_knownfortitles_t
    - titles_t
  join_type: MANY_TO_ONE
  condition: names_knownfortitles_t.title_id = titles_t.id

# 10. names_primaryprofessions → professions（人物职业）
- name: names_primaryprofessions_t_professions_t
  models:
    - names_primaryprofessions_t
    - professions_t
  join_type: MANY_TO_ONE
  condition: names_primaryprofessions_t.profession_id = professions_t.id

# 11. titleakas → titles（别名-作品）
- name: titleakas_t_titles_t
  models:
    - titleakas_t
    - titles_t
  join_type: MANY_TO_ONE
  condition: titleakas_t.title_id = titles_t.id

# 12. principals_characters → principals（角色-参与者）
- name: principals_characters_t_principals_t_v2
  models:
    - principals_characters_t
    - principals_t
  join_type: MANY_TO_ONE
  condition: principals_characters_t.principal_id = principals_t.id
```

---

## 3、深度测试问题（6大类 × 3题 = 18题）

### 类别 A：业务指标计算（含 KPI 公式）

**A1. 计算每部电影的综合质量得分（Quality Score），定义为：**
```
Quality Score = average_rating × LOG(num_votes + 1) × (1 + 0.1 × (runtime_minutes / 120 - 1))
```
要求：找出质量得分最高的 20 部电影，列出片名、年份、评分、投票数、片长和质量得分。

**考察点：** 复杂数学公式翻译、LOG 函数、多字段复合计算、NULL 处理

---

**A2. 计算每位导演的"票房号召力指数"（Star Power Index）：**
```
Star Power Index = AVG(导演作品评分) × LOG(SUM(导演作品投票数)) × (导演作品数 / 导演职业生涯跨度)
```
要求：找出 Star Power Index > 50 的导演，按指数降序排列。

**考察点：** 多层嵌套聚合、子查询、HAVING 过滤、多表 JOIN（names→principals→titles）、业务指标理解

---

**A3. 计算各类型电影的"投资回报率"（ROI）指标：**
```
ROI = (该类型平均评分 - 整体平均评分) / 整体平均评分 × (该类型平均投票数 / 整体平均投票数)
```
要求：列出所有类型的 ROI 值，按 ROI 降序排列，并标注该类型电影数量。

**考察点：** 窗口函数（整体平均）、跨行计算、复合指标、多表 JOIN

---

### 类别 B：时间序列与趋势分析

**B1. 计算每个年代（decade）各类型电影的"市场份额"变化：**
```
市场份额 = 该年代某类型电影数 / 该年代电影总数 × 100%
```
要求：找出市场份额变化最大的 3 个类型（1990s → 2000s → 2010s 的变化幅度）。

**考察点：** 多级 GROUP BY、百分比计算、年代间差值计算、LAG/LEAD 窗口函数

---

**B2. 分析电视剧系列的"评分生命周期"：**
```
每季评分变化率 = (该季评分 - 上一季评分) / 上一季评分 × 100%
```
要求：找出评分在第 3 季之后持续下滑（连续 2 季负增长）的电视剧系列，列出系列名、各季评分和变化率。

**考察点：** 剧集层级查询（series→episodes→titles）、LAG 窗口函数、多条件过滤、趋势判断逻辑

---

**B3. 计算演员的"黄金期"（Golden Period）：**
```
黄金期 = 连续 3 年内参演作品平均评分 > 该演员整体平均评分的时期
```
要求：找出黄金期最长的前 10 位演员，列出姓名、黄金期起止年份、黄金期平均评分。

**考察点：** 自关联/窗口函数实现滑动窗口、时间范围比较、多层子查询、复杂业务逻辑

---

### 类别 C：多维下钻分析（Cube 风格）

**C1. 按【年代 × 评分等级 × 片长分类】三维下钻，统计电影数量和平均评分：**
要求：找出"1990年代 + 高分(8-10) + 长片(90-150min)"这个组合下的 Top 10 电影。

**考察点：** 多维度 CASE WHEN 分类、多维聚合、组合条件过滤

---

**C2. 分析"高热度 + 低评分"的异常影片特征：**
```
异常定义：num_votes > 50000（高热度）且 average_rating < 5.0（低评分）
```
要求：找出这类异常影片，按类型分布统计数量，并与"高热度 + 高评分"影片的特征做对比。

**考察点：** 异常检测思维、条件分组对比、多集合 UNION/条件聚合

---

**C3. 按出生年代分析导演的创作力峰值：**
```
创作力峰值年代 = 导演产出最多作品的年代（以 decade 计）
```
要求：统计 1960s、1970s、1980s 出生的导演，其创作力峰值分别集中在哪个年代，以及峰值期的平均评分。

**考察点：** 多表 JOIN、双层 GROUP BY、子查询找峰值、年代交叉分析

---

### 类别 D：人物关系与网络分析

**D1. 找出"黄金搭档"（频繁合作的导演-演员组合）：**
```
合作次数 ≥ 3 次，且合作作品平均评分 ≥ 7.5
```
要求：列出 Top 10 黄金搭档，显示导演名、演员名、合作次数、合作作品平均评分。

**考察点：** 自关联 principals 表（同一作品找导演+演员）、多条件 HAVING、复杂 JOIN 逻辑

---

**D2. 找出"六度分隔"中的超级连接者（连接最多不同导演的演员）：**
```
定义：与 ≥ 20 位不同导演合作过的演员
```
要求：列出这些超级连接者，显示姓名、合作导演数、参演电影数。

**考察点：** 多表 JOIN、COUNT(DISTINCT)、子查询、社交网络分析思维

---

**D3. 分析"演而优则导"的跨界者表现：**
```
定义：同时有 actor/actress 和 director 身份的人
```
要求：对比其作为演员的作品平均评分 vs 作为导演的作品平均评分，计算跨界效果（导演评分 - 演员评分），按跨界效果降序排列。

**考察点：** 同一表按 category 条件聚合、PIVOT 风格查询、差值计算、多角色身份识别

---

### 类别 E：异常检测与数据质量

**E1. 检测"评分异常"影片（贝叶斯校正）：**
```
加权评分 = (num_votes × average_rating + C × global_avg) / (num_votes + C)
其中 C = 1000（最小可信投票数）, global_avg = 所有电影平均评分
```
要求：找出加权评分与原始评分差异最大的 20 部电影（即投票数少导致评分不可信的影片）。

**考察点：** 贝叶斯平滑公式、窗口函数求全局平均、差值排序、数据质量意识

---

**E2. 检测"长寿剧集"的评分衰退模式：**
```
衰退率 = (第1季平均评分 - 最后一季平均评分) / 第1季平均评分
```
要求：找出衰退率 > 30% 的电视剧系列，分析其各季评分变化轨迹。

**考察点：** 剧集层级查询、MIN/MAX 季号定位首末季、百分比变化计算

---

**E3. 发现"隐藏的宝藏片"：**
```
定义：average_rating ≥ 8.5 且 num_votes < 5000（高质量但未被广泛关注）
```
要求：按类型分布统计这些宝藏片，并计算每个类型中宝藏片占该类型电影总数的比例。

**考察点：** 多条件组合、双重聚合（先计数再算比例）、类型分组

---

### 类别 F：综合商业分析场景

**F1. 构建"电影投资决策评分卡"：**
```
投资评分 = 
  0.35 × (average_rating / 10) + 
  0.25 × (LOG(num_votes + 1) / LOG(MAX_VOTES + 1)) + 
  0.20 × (1 - ABS(runtime_minutes - 120) / 120) + 
  0.20 × (CASE WHEN is_adult = 0 THEN 1 ELSE 0.3 END)
```
要求：计算每部电影的投资评分，按评分降序排列 Top 50，并分析高分影片的共同特征。

**考察点：** 加权评分模型、多维度归一化、业务决策模拟、窗口函数求 MAX_VOTES

---

**F2. 分析"演员阵容强度"对电影评分的影响：**
```
阵容强度 = 参演演员中，个人平均作品评分 > 7.0 的演员数量 / 总参演演员数
```
要求：按阵容强度分档（强≥0.6、中0.3-0.6、弱<0.3），统计各档电影的平均评分和数量。

**考察点：** 多层嵌套子查询（先算演员个人平均分，再算电影阵容强度，再分档聚合）、复杂业务逻辑

---

**F3. 预测系列剧的"最佳完结时机"：**
```
定义：某季评分首次低于该剧平均评分的 80% 时，即为"应完结"信号
```
要求：找出那些已经播出超过"应完结"信号季数但仍继续的电视剧，列出系列名、当前季数、信号出现季数、超出季数。

**考察点：** 剧集层级分析、窗口函数计算累计平均、阈值比较、业务决策支持

---

## 4、测试评分矩阵

### 4.1 评分维度（每项 1-5 分）

| 维度 | 权重 | 说明 |
|------|------|------|
| **SQL 复杂度** | 20% | JOIN 数量、子查询层级、窗口函数、CTE 使用 |
| **Schema Linking 难度** | 20% | 需要理解多少表关系、字段语义、隐含关联 |
| **业务理解深度** | 25% | 是否需要领域知识、指标公式理解、业务规则 |
| **NL 语义解析难度** | 15% | 自然语言描述的模糊度、歧义性、隐含条件 |
| **结果正确性验证** | 10% | 是否容易人工验证、是否有明确预期值 |
| **实际业务价值** | 10% | 问题是否贴近真实分析场景、是否有洞察价值 |

### 4.2 评分矩阵

| 题号 | SQL复杂度 | Schema Linking | 业务理解 | NL解析 | 结果验证 | 业务价值 | 加权总分 |
|------|-----------|---------------|----------|--------|----------|----------|----------|
| A1   | 4 | 2 | 5 | 4 | 3 | 5 | **3.95** |
| A2   | 5 | 4 | 5 | 4 | 3 | 5 | **4.50** |
| A3   | 5 | 3 | 5 | 4 | 4 | 4 | **4.30** |
| B1   | 4 | 3 | 4 | 3 | 5 | 4 | **3.80** |
| B2   | 5 | 4 | 5 | 4 | 4 | 5 | **4.55** |
| B3   | 5 | 4 | 5 | 5 | 3 | 4 | **4.55** |
| C1   | 3 | 3 | 3 | 3 | 5 | 3 | **3.20** |
| C2   | 4 | 2 | 4 | 3 | 4 | 4 | **3.50** |
| C3   | 4 | 4 | 4 | 4 | 4 | 4 | **4.00** |
| D1   | 5 | 5 | 4 | 4 | 4 | 5 | **4.55** |
| D2   | 4 | 4 | 4 | 3 | 4 | 4 | **3.85** |
| D3   | 5 | 4 | 5 | 4 | 4 | 5 | **4.55** |
| E1   | 5 | 2 | 5 | 4 | 4 | 4 | **4.15** |
| E2   | 4 | 4 | 4 | 4 | 4 | 4 | **4.00** |
| E3   | 3 | 2 | 3 | 3 | 5 | 4 | **3.10** |
| F1   | 5 | 3 | 5 | 5 | 3 | 5 | **4.55** |
| F2   | 5 | 5 | 5 | 5 | 3 | 5 | **4.80** |
| F3   | 5 | 4 | 5 | 5 | 3 | 5 | **4.65** |

### 4.3 难度分级

| 级别 | 分数范围 | 题号 |
|------|----------|------|
| ⭐ 基础 | < 3.5 | C1, C2, E3 |
| ⭐⭐ 中等 | 3.5 - 4.2 | A1, A3, B1, C3, D2, E1, E2 |
| ⭐⭐⭐ 困难 | 4.2 - 4.6 | A2, B2, B3, D1, D3, F1, F3 |
| ⭐⭐⭐⭐ 极难 | > 4.6 | F2 |

---

## 5、执行方案

### 5.1 推荐执行顺序

```
Phase 1 — 连通性验证（30分钟）
  → A1（基础公式计算）
  → E3（简单条件过滤）
  → C1（多维分类）

Phase 2 — 核心能力验证（60分钟）
  → A2（多表 JOIN + 聚合）
  → D1（自关联 JOIN）
  → B1（时间序列 + 窗口函数）

Phase 3 — 高级挑战（90分钟）
  → F2（多层嵌套 + 复杂业务逻辑）
  → B2（剧集层级 + 趋势分析）
  → D3（PIVOT 风格 + 跨界分析）

Phase 4 — 极限挑战（60分钟）
  → F1（加权评分模型 + 归一化）
  → F3（阈值比较 + 决策支持）
  → B3（滑动窗口 + 黄金期检测）
```

### 5.2 评估指标

| 指标 | 目标值 | 说明 |
|------|--------|------|
| SQL 生成成功率 | ≥ 85% | 生成的 SQL 语法正确且能执行 |
| 语义准确率 | ≥ 80% | SQL 语义与自然语言需求一致 |
| 结果正确率 | ≥ 75% | 查询结果符合业务预期 |
| 复杂查询（5+ JOIN）成功率 | ≥ 70% | 多表关联场景的表现 |
| 指标计算准确率 | ≥ 80% | 公式翻译的准确性 |
| 平均响应时间 | ≤ 15s | 从提问到返回结果的时间 |

---

## 六、测试报告模板

```markdown
## 测试报告：问题 [编号]

### 基本信息
- **问题描述：** [原始 NL 问题]
- **难度等级：** ⭐⭐⭐
- **涉及表：** [表1, 表2, ...]

### 执行结果
- **SQL 生成：** ✅ / ❌
- **SQL 执行：** ✅ / ❌
- **结果正确性：** ✅ / ⚠️ / ❌
- **响应时间：** [X]s

### 生成的 SQL
​```sql
[实际生成的 SQL]
```

### 结果摘要
[前 5 行结果展示]

### 问题分析
- **成功/失败原因：** [分析]
- **改进建议：** [建议]
```

---

## 七、预期成果

1. **全面验证 NL2SQL 引擎能力边界** — 从简单过滤到复杂指标计算
2. **发现典型失败模式** — Schema Linking 错误、公式翻译错误、多表 JOIN 遗漏
3. **量化评估 NL2SQL 质量** — 按 6 个维度打分，形成能力雷达图
4. **积累业务知识库** — 通过 knowledge.yml 沉淀领域规则，持续优化

```