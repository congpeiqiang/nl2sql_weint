# IMDb 术语表

## 一、核心表术语

### titles（作品表）
| 字段 | 类型 | 含义 | 业务说明 |
|------|------|------|---------|
| id | VARCHAR | IMDb 唯一作品标识 | 格式：tt + 7-8 位数字，如 tt1375666 |
| title_type | VARCHAR | 作品类型 | movie/short/tvSeries/tvEpisode/tvMovie/tvMiniSeries/tvSpecial/video/videoGame |
| primary_title | VARCHAR | 主要标题 | 通常为原始语言标题 |
| original_title | VARCHAR | 原始标题 | 可能与 primary_title 相同 |
| is_adult | INTEGER | 成人内容标记 | 0=否, 1=是 |
| start_year | INTEGER | 首映/发布年份 | 电影为上映年，剧集为播出年 |
| end_year | INTEGER | 终映年份 | 电视剧系列专用，电影为 NULL |
| runtime_minutes | INTEGER | 片长（分钟） | 剧集通常为 NULL |
| average_rating | DOUBLE | IMDb 加权平均分 | 范围 1.0-10.0 |
| num_votes | INTEGER | 用户投票总数 | 越高越可信 |

### names（人物表）
| 字段 | 类型 | 含义 | 业务说明 |
|------|------|------|---------|
| id | VARCHAR | IMDb 唯一人物标识 | 格式：nm + 7-8 位数字 |
| primary_name | VARCHAR | 全名 | 最常用拼写形式 |
| birth_year | INTEGER | 出生年份 | |
| death_year | INTEGER | 逝世年份 | NULL = 仍健在 |

### principals（参与者表）
| 字段 | 类型 | 含义 | 业务说明 |
|------|------|------|---------|
| id | INTEGER | 自增主键 | |
| title_id | VARCHAR | 作品 ID | 关联 titles.id |
| name_id | VARCHAR | 人物 ID | 关联 names.id |
| ordering | INTEGER | 重要性排序 | 1=最主要角色，值越小越重要 |
| category_id | VARCHAR | 职位类别 | actor/actress/director/writer/producer/composer/cinematographer/editor |
| job_id | VARCHAR | 具体职位 | 更细化的职位描述，关联 jobs.code |

### episodes（剧集关联表）
| 字段 | 类型 | 含义 | 业务说明 |
|------|------|------|---------|
| id | VARCHAR | 剧集 ID | 关联 titles.id（作为剧集） |
| parent_id | VARCHAR | 父系列 ID | 关联 titles.id（作为系列） |
| season_number | INTEGER | 季号 | 第几季 |
| episode_number | INTEGER | 集号 | 第几集 |

## 二、维度表术语

| 表名 | 字段 | 含义 |
|------|------|------|
| genres | id | 类型标识符（字符串）：action, comedy, drama, horror, sci-fi 等 |
| genres | display_name | 类型显示名 |
| categories | code | 职位类别代码：actor, actress, director, writer 等 |
| categories | display_name | 类别显示名 |
| jobs | code | 具体职位代码（约 3 万种自由格式） |
| jobs | display_name | 职位显示名 |
| professions | id | 职业名称：actor, director, producer, writer 等 |
| professions | display_name | 职业显示名 |

## 三、关联表术语

| 表名 | 含义 | 说明 |
|------|------|------|
| titles_genres | 作品-类型多对多关联 | 联合主键 (title_id, genre_id) |
| principals_characters | 参与者-角色名关联 | 记录演员在作品中饰演的角色名 |
| names_primaryprofessions | 人物-主要职业关联 | 记录每个人的主要职业 |
| names_knownfortitles | 人物-代表作关联 | 记录每个人最知名的 1-4 部作品 |
| titleakas | 作品别名表 | 非原始语言标题/地区特定名称 |

## 四、业务概念术语

| 术语 | 含义 | 计算方式 |
|------|------|---------|
| 演员 | 表演者 | category_id IN ('actor', 'actress') |
| 主要演员 | 主演 | category_id IN ('actor','actress') AND ordering <= 3 |
| 导演 | 影片导演 | category_id = 'director' |
| 主创团队 | 核心创作人员 | ordering <= 3 |
| 年代 (decade) | 十年跨度 | FLOOR(start_year / 10) * 10 |
| 职业生涯跨度 | 从业年数 | MAX(start_year) - MIN(start_year) |
| 活跃密度 | 年均产出 | COUNT(DISTINCT title_id) / career_span |
| 多面手得分 | 角色多样性 | COUNT(DISTINCT category_id) |
| 热度指数 | 相对热度 | num_votes / 同类型平均投票数 |
| 黄金期 | 创作巅峰期 | 连续 3 年平均评分 > 个人整体平均 |
| 跨界者 | 多角色从业者 | 同时拥有 actor+director 等身份 |
| 宝藏片 | 被忽视的好片 | rating >= 8.5 AND votes < 5000 |
| 评分衰退率 | 质量下滑幅度 | (首季评分 - 末季评分) / 首季评分 |
| 国际影响力 | 不同国家的别名数量 | 该作品在非原始语言地区的别名数量 × 覆盖的不同区域数量 |
