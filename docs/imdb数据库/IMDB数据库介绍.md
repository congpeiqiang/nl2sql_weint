# IMDb 数据库介绍（rmdb）

> 来源: https://github.com/combatwombat/rmdb
> rmdb 将 IMDb 官方开放数据集导入 MySQL/MariaDB 关系型数据库
> 数据集地址: https://datasets.imdbws.com

---

## 数据库概述

IMDb（Internet Movie Database）数据库存储的核心数据可以概括为：**全球影视作品、从业者的结构化信息，以及由海量观众产生的评分和热度数据**。
rmdb 项目将 IMDb 的 TSV 格式数据集转为你可以直接查询的关系型数据库。

**数据规模：** 下载约 1.5GB，导入后约 7GB+

### 安装步骤

```bash
# 1. 创建 MySQL 数据库，导入 schema
mysql -u root -p -e "CREATE DATABASE imdb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
mysql -u root -p imdb < config/schema.sql

# 2. 配置数据库连接
# 编辑 config/config.json，填入数据库信息

# 3. 下载并导入数据
php rmdb/public/index.php download   # 下载 TSV 文件（~1.5GB）
php rmdb/public/index.php extract    # 解压（~7GB）
php rmdb/public/index.php import     # 导入 MySQL（约1小时）
```

---

## 数据表一览（17张表）

### 核心表

| 表名 | 描述 | 大约记录数 |
|------|------|-----------|
| `titles` | 所有电影、短片、电视剧集 | ~1000万+ |
| `names` | 所有影视从业者（演员、导演等） | ~1200万+ |
| `principals` | 核心参与者关联（人名→作品→角色→排序） | ~5000万+ |
| `ratings` | 作品评分 | 包含在 titles 中 |

### 类型与分类

| 表名 | 描述 |
|------|------|
| `genres` | 所有类型标签：Action、Sci-Fi、Comedy 等 |
| `titles_genres` | 作品→类型 多对多映射 |
| `categories` | 主要职位类型：Actor、Actress、Director 等 |
| `jobs` | 自由格式职位名称（~3万种） |

### 别名与多语言

| 表名 | 描述 |
|------|------|
| `titleakas` | 作品的外语名称/别名 |
| `titleakaattributes` | 别名属性：Fake Working Title、Berlin Festival Title |
| `titleakatypes` | 别名类型：Alternative、Working、IMDb Display |
| `titleakas_titleakaattributes` | 别名→属性 映射 |
| `titleakas_titleakatypes` | 别名→类型 映射 |

### 人物关联

| 表名 | 描述 |
|------|------|
| `principals_characters` | 演员→角色名 关联 |
| `professions` | 主要职业：Producer、Stunts、casting Director |
| `names_primaryprofessions` | 人物→职业 映射 |
| `names_knownfortitles` | 人物→代表作 关联 |

### 剧集

| 表名 | 描述 |
|------|------|
| `episodes` | 剧集→所属系列 关联 |

---

## 核心表 Schema

## 完整表 Schema 详解

### 1. titles — 作品表

IMDb 的核心表，包含所有电影、短片、电视剧集、电视系列等作品信息。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | VARCHAR(12) | PK | IMDb 唯一标识，格式如 `tt0111161`（电影）、`tt0944947`（剧集） |
| `title_type` | VARCHAR(20) | | 作品类型：`movie`(电影)、`short`(短片)、`tvEpisode`(电视剧集)、`tvSeries`(电视系列)、`tvMovie`(电视电影)、`tvMiniSeries`(迷你剧)、`tvSpecial`(特别节目)、`video`(视频)、`videoGame`(游戏) |
| `primary_title` | TEXT | | 主要标题（通常为原始语言标题，不会和 original_title 有很大差异） |
| `original_title` | TEXT | | 原始标题（与 primary_title 可能相同） |
| `is_adult` | BOOLEAN | | 是否成人内容，`0`=否，`1`=是 |
| `start_year` | INTEGER | ✓ | 首映/发布年份（电视剧集为所属季的年份） |
| `end_year` | INTEGER | ✓ | 终映年份（电视剧/系列使用，电影通常为 NULL） |
| `runtime_minutes` | INTEGER | ✓ | 片长（分钟），电视剧集通常为 NULL |
| `average_rating` | FLOAT | ✓ | IMDb 加权平均评分（1.0 - 10.0），来源: `title.ratings.tsv.gz` |
| `num_votes` | INTEGER | ✓ | 用户投票总数，投票数越高评分越可信 |

**数据量约：** 1000万+ 条 | **注意：** 评分和投票数已从 ratings 表合并到此表

---

### 2. names — 人物表

包含所有与影视作品相关的人员信息：演员、导演、编剧等。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | VARCHAR(12) | PK | IMDb 唯一标识，格式如 `nm0000138`（Leonardo DiCaprio） |
| `primary_name` | TEXT | | 全名（通常采用最常用的拼写形式） |
| `birth_year` | INTEGER | ✓ | 出生年份 |
| `death_year` | INTEGER | ✓ | 逝世年份，仍健在则为 `NULL` |

**数据量约：** 1200万+ 条 | **用途：** 查询演员、追踪人物生涯跨度

---

### 3. genres — 电影类型表

IMDb 标准电影类型标签。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | VARCHAR(50) | PK | 类型标识符，值如 `action`、`comedy`、`drama`、`horror`、`sci-fi` 等 |

**数据量约：** 28 种 | **注意：** genre_id 存储的是类型名称字符串，不是数字ID

常见类型：`action`, `adult`, `adventure`, `animation`, `biography`, `comedy`, `crime`, `documentary`, `drama`, `family`, `fantasy`, `film-noir`, `game-show`, `history`, `horror`, `music`, `musical`, `mystery`, `news`, `reality-tv`, `romance`, `sci-fi`, `short`, `sport`, `talk-show`, `thriller`, `war`, `western`

---

### 4. titles_genres — 作品-类型关联表

多对多关系表，一个作品可以有多个类型标签。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `title_id` | VARCHAR(12) | FK | 作品ID，关联 `titles.id` |
| `genre_id` | VARCHAR(50) | FK | 类型ID，关联 `genres.id` |

**联合主键:** `(title_id, genre_id)` | **查询提示：** 分析多类型的作品需要 JOIN 两次此表

---

### 5. categories — 职位类别表

作品中的职位类型分类，区别于 jobs 表的自由格式职位名称。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `code` | VARCHAR(128) | PK | 类别标识符，如 `actor`、`director` 等 |

**包含类别：** `actor`, `actress`, `archive_footage`, `archive_sound`, `cinematographer`, `composer`, `director`, `editor`, `producer`, `production_designer`, `self`, `writer`

---

### 6. jobs — 职位表

自由格式的职位名称，比 categories 更细化（约 3 万种）。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `code` | VARCHAR(128) | PK | 职位名称，如 `Act Three written By`、`Live Show Editor` 等 |

**注意：** IMDb 的职位名称是自由文本，没有标准化的层级结构

---

### 7. principals — 核心参与者表

最核心的人物-作品关联表，记录每部作品的关键参与者。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | INTEGER | PK | 自增主键 |
| `title_id` | VARCHAR(12) | FK | 作品ID，关联 `titles.id` |
| `ordering` | INTEGER | | 排序号，`1`=最主要的角色/参与者，值越小越重要 |
| `name_id` | VARCHAR(12) | FK | 人物ID，关联 `names.id` |
| `category_id` | VARCHAR(50) | FK | 职位类别，关联 `categories.id`（如 `actor`、`director`） |
| `job_id` | VARCHAR(200) | FK | 具体职位，关联 `jobs.id`（更细化的职位描述） |

**数据量约：** 5000万+ 条 | **查询提示：** `category_id` 区分职位类型，`ordering=1` 获取最主要角色

---

### 8. principals_characters — 角色名关联表

将 principals 记录与角色名一对一关联（principals.characters 字段可能包含 JSON 数组）。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | INTEGER | PK | 自增主键 |
| `principal_id` | INTEGER | FK | 关联 `principals.id` |
| `character` | TEXT | | 角色名称 |

---

### 9. professions — 职业表

人物的主要职业分类（来源: name.basics 数据集的 primaryProfession 字段）。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | VARCHAR(100) | PK | 职业名称 |

**包含职业：** `actor`, `actress`, `animation_department`, `art_department`, `art_director`, `assistant`, `camera_department`, `casting_department`, `casting_director`, `cinematographer`, `composer`, `costume_department`, `costume_designer`, `director`, `editor`, `editorial_department`, `electrical_department`, `executive`, `legal`, `location_management`, `make_up_department`, `manager`, `miscellaneous`, `music_artist`, `music_department`, `podcaster`, `producer`, `production_department`, `production_designer`, `production_manager`, `publicist`, `script_department`, `sound_department`, `soundtrack`, `special_effects`, `stunts`, `talent_agent`, `transportation_department`, `visual_effects`, `writer`

---

### 10. names_primaryprofessions — 人物-职业关联表

多对多关系表，记录每个人的主要职业。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `name_id` | VARCHAR(12) | FK | 人物ID，关联 `names.id` |
| `profession_id` | VARCHAR(100) | FK | 职业ID，关联 `professions.id` |

**联合主键:** `(name_id, profession_id)` | **查询提示：** 找导演用 `profession_id = 'director'`

---

### 11. names_knownfortitles — 人物代表作表

记录每个人最知名的作品（通常 1-4 部）。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `name_id` | VARCHAR(12) | FK | 人物ID，关联 `names.id` |
| `title_id` | VARCHAR(12) | FK | 作品ID，关联 `titles.id` |

**联合主键:** `(name_id, title_id)`

---

### 12. titleakas — 作品别名表

作品的非原始语言标题、其他名称、地区特定名称。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | INTEGER | PK | 自增主键 |
| `title_id` | VARCHAR(12) | FK | 作品ID，关联 `titles.id` |
| `ordering` | INTEGER | | 排序号（1=主要别名） |
| `title` | TEXT | | 别名标题文本 |
| `region` | VARCHAR(10) | ✓ | 使用该别名的地区（ISO 3166-1 alpha-2），如 `US`、`CN`、`FR` |
| `language` | VARCHAR(10) | ✓ | 别名语言（ISO 639-1），如 `en`、`zh`、`fr` |
| `is_original_title` | BOOLEAN | | 是否为原始标题，`0`/`1` |

**数据量约：** 数百万条 | **注意：** region 和 language 可为 NULL（未指定）

---

### 13. titleakaattributes — 别名属性表

别名类型的属性标记。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | VARCHAR(100) | PK | 属性标识符 |

**包含属性：** `alternative`, `dvd`, `festival`, `fake_working_title`, `imdb_display`, `tv`, `working`, `script`

---

### 14. titleakatypes — 别名类型表

较小范围的别名分类。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | VARCHAR(100) | PK | 类型标识符 |

**包含类型：** `alternative`, `working`, `imdb_display` 等

---

### 15. titleakas_titleakaattributes — 别名-属性关联表

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `titleaka_id` | INTEGER | FK | 别名ID，关联 `titleakas.id` |
| `titleakaattribute_id` | VARCHAR(100) | FK | 属性ID，关联 `titleakaattributes.id` |

---

### 16. titleakas_titleakatypes — 别名-类型关联表

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `titleaka_id` | INTEGER | FK | 别名ID，关联 `titleakas.id` |
| `titleakatype_id` | VARCHAR(100) | FK | 类型ID，关联 `titleakatypes.id` |

---

### 17. episodes — 剧集关联表

连接电视剧集与其所属的系列。

| 字段 | 类型 | NULL | 描述 |
|------|------|------|------|
| `id` | INTEGER | PK | 自增主键 |
| `title_id` | VARCHAR(12) | FK | 剧集作品ID，关联 `titles.id`（具体某一集） |
| `parent_title_id` | VARCHAR(12) | FK | 所属系列ID，关联 `titles.id`（整个系列，如 Game of Thrones） |
| `season_number` | INTEGER | ✓ | 季号 |
| `episode_number` | INTEGER | ✓ | 集号 |

**查询提示：** 查找所有季所有集：`WHERE parent_title_id = 'tt0944947'`

---

## ER 关系图

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│   genres    │     │  titles_genres   │     │   titles    │
│──────────── │◄────│───────────────── │────►│──────────── │
│ id          │     │ title_id (FK)    │     │ id (PK)     │
│ (genre name)│     │ genre_id (FK)    │     │ title_type  │
└─────────────┘     └──────────────────┘     │ primary_title│
                                              │ start_year  │
┌─────────────┐     ┌──────────────────┐     │ avg_rating  │
│ categories  │     │   principals     │     │ num_votes   │
│──────────── │◄────│───────────────── │────►└──────┬──────┘
│ id          │     │ id (PK)         │            │
│ (category)  │     │ title_id (FK) ◄─┼────────────┘
└─────────────┘     │ ordering        │      ┌──────┴──────┐
                    │ name_id (FK) ◄──┼──────┤    names    │
┌─────────────┐     │ category_id (FK)│      │─────────────│
│    jobs     │     │ job_id (FK) ◄───┼──┐   │ id (PK)     │
│──────────── │◄────│ characters      │  │   │ primary_name│
│ id          │     └────────┬────────┘  │   │ birth_year  │
│ (job title) │              │           │   │ death_year  │
└─────────────┘     ┌────────┴────────┐  │   └─────────────┘
                    │principals_chars │  │
                    │─────────────── │  │   ┌──────────────┐
                    │ principal_id   │  │   │  professions │
                    │ character      │  │   │──────────────│
                    └────────────────┘  │   │ id           │
                                        │   └──────┬───────┘
┌─────────────┐                         │   ┌──────┴───────────┐
│  episodes   │                         │   │names_primaryprof │
│─────────────│                         │   │──────────────────│
│ id (PK)     │                         │   │ name_id (FK)     │
│ title_id ◄──┼──┐                      │   │ profession_id(FK)│
│ parent_id ◄─┼──┤                      │   └──────────────────┘
│ season_num  │  │                      │
│ episode_num │  │                      │   ┌──────────────────┐
└─────────────┘  │                      │   │names_knownfortitl│
                 │                      │   │──────────────────│
                 └────► titles          └───│ name_id (FK)     │
                                            │ title_id (FK)    │
┌─────────────┐                             └──────────────────┘
│  titleakas  │      ┌──────────────────┐
│──────────── │─────►│titleakas_titleaka│
│ id (PK)     │      │──────────────────│
│ title_id ◄──┼──┐   │ titleaka_id (FK) │
│ title       │  │   │ attr/type_id(FK) │
│ region      │  │   └──────────────────┘
│ language    │  │
└─────────────┘  │
                 └────► titles
```

---

## 核心表简要字段

### titles — 作品表

| 字段 | 类型 | 描述 |
|------|------|------|
| `id` | VARCHAR(12) PK | tt 开头 7-8位ID |
| `title_type` | VARCHAR(20) | movie / short / tvEpisode / tvSeries 等 |
| `primary_title` | TEXT | 原始语言片名 |
| `original_title` | TEXT | 原始片名 |
| `is_adult` | BOOLEAN | 是否成人内容 |
| `start_year` | INTEGER | 首映年份 |
| `end_year` | INTEGER | 终映年份（剧集） |
| `runtime_minutes` | INTEGER | 片长（分钟） |
| `average_rating` | FLOAT | IMDb 平均评分（1-10） |
| `num_votes` | INTEGER | 投票数 |

### names — 人物表

| 字段 | 类型 | 描述 |
|------|------|------|
| `id` | VARCHAR(12) PK | nm 开头 7-8位ID |
| `primary_name` | TEXT | 全名 |
| `birth_year` | INTEGER | 出生年份 |
| `death_year` | INTEGER | 逝世年份（NULL=在世） |

### principals — 核心参与者

| 字段 | 类型 | 描述 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `title_id` | FK → titles.id | 作品ID |
| `name_id` | FK → names.id | 人物ID |
| `ordering` | INTEGER | 排序（1=最重要） |
| `category_id` | FK → categories.id | 职位类型 |
| `job_id` | FK → jobs.id | 具体职位 |

### episodes — 剧集表

| 字段 | 类型 | 描述 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `title_id` | FK → titles.id | 剧集ID |
| `parent_title_id` | FK → titles.id | 所属系列ID |
| `season_number` | INTEGER | 季号 |
| `episode_number` | INTEGER | 集号 |

---

## NL2SQL 适用场景

### 推荐查询类型

1. **电影筛选** — "评分 8 分以上、2000 年后的动作片"
2. **排行榜** — "60 年代最高产的导演 Top 10"
3. **类型分析** — "科幻电影的评分趋势"
4. **关联查询** — "斯皮尔伯格导演的评分最高电影"
5. **多表聚合** — "各类型电影每年发布数量变化"

### 不适合查询的类型

- 实时票房数据（IMDb 不含此数据）
- 电影情节/简介全文搜索
- 用户个人评分数据

---

## SQL 示例

### 1. 各年代类型分布

```sql
SELECT start_year, COUNT(*) AS count
FROM titles
LEFT JOIN titles_genres AS tg ON tg.title_id = titles.id
WHERE tg.genre_id = 'drama'
  AND title_type = 'movie'
  AND start_year < YEAR(CURRENT_DATE())
GROUP BY start_year
ORDER BY start_year ASC;
```

### 2. 60 年代最高分电影的高产导演 Top 10

```sql
SELECT n.primary_name AS name, COUNT(*) AS movies
FROM principals
LEFT JOIN names AS n ON n.id = name_id
LEFT JOIN titles AS t ON t.id = title_id
WHERE category_id = 'director'
  AND t.title_type = 'movie'
  AND t.start_year BETWEEN 1960 AND 1969
  AND t.runtime_minutes >= 90
  AND t.average_rating > 6
  AND t.num_votes > 10000
GROUP BY name_id
ORDER BY movies DESC
LIMIT 10;
```

### 3. 高分恐怖喜剧 Top 50

```sql
SELECT primary_title, start_year, average_rating, num_votes
FROM titles
LEFT JOIN titles_genres tg1 ON tg1.title_id = titles.id
LEFT JOIN titles_genres tg2 ON tg2.title_id = titles.id
WHERE tg1.genre_id = 'horror'
  AND tg2.genre_id = 'comedy'
  AND title_type = 'movie'
  AND num_votes > 20000
ORDER BY average_rating DESC, num_votes DESC
LIMIT 50;
```

---

## 注意事项

1. **数据量巨大** — 导入后约 7GB+，建议在测试机部署
2. **评分字段在 titles 表中** — 不是独立表，和其他字段一起查询即可
3. **genre 存储为字符串** — `genre_id` 直接存 'horror'、'comedy' 等值，非外键数字
4. **人物关联复杂** — 一个人可以有多重角色（既是导演又是演员），需要按 category_id 过滤
5. **剧集排除** — 很多查询需要 `title_type = 'movie'` 排除剧集
