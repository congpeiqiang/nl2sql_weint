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
titles ←→ principals ←→ names
    ↓                           ↓
titles_genres          names_primaryprofessions
    ↓                           ↓
genres                 professions

# 剧集路径
titles (series) ←→ episodes ←→ titles (episode)

# 别名路径
titles ←→ titleakas

# 角色路径
principals ←→ principals_characters
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
