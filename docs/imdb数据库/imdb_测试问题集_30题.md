# IMDb 数据库 — NL2SQL 测试问题集（30题）

> **生成时间**: 2026-07-30  
> **数据来源**: IMDb (MySQL) — WrenAI 语义层  
> **数据库说明**: 基于 IMDb 公开数据集，包含 titles（作品表）、names（人物表）、principals（参与者表）、episodes（剧集表）、genres（类型表）等 17 张表。

---

## 难度分级说明

| 级别 | 说明 | 涉及技能 |
|:----:|------|---------|
| ⭐ 简单 | 单表查询，简单过滤/排序/聚合 | SELECT, WHERE, ORDER BY, GROUP BY, COUNT, AVG |
| ⭐⭐ 中等 | 2-3 表 JOIN，带条件聚合，分组统计 | JOIN, HAVING, 多条件过滤, 子查询 |
| ⭐⭐⭐ 较难 | 多表 JOIN，窗口函数，复杂业务逻辑 | 窗口函数, CTE, CASE WHEN, 多步聚合 |
| ⭐⭐⭐⭐ 困难 | 高级分析指标，多步推理，业务语义理解 | 贝叶斯校正, 百分比计算, 趋势分析, 多维度交叉分析 |

---

## 一、⭐ 简单问题（8题）

### Q1：查询评分最高的 10 部电影
- **问题**: 查询 average_rating 最高的 10 部电影，显示标题和评分
- **涉及表**: titles
- **过滤条件**: title_type = 'movie'
- **验证方法**: 直接看 rating 降序排列，前 10 条
- **预期结果**: 返回 10 条记录，评分从高到低
- **测试结果**
  - 测试报告D:\code_work_space\llm\nl2sql\src\agent\workspace\report\Top10_Movies_by_Average_Rating_2026-07-31_10-34-07.md

### Q2：查询 2020 年之后上映的电影数量
- **问题**: 2020 年之后（含 2020）共有多少部电影？
- **涉及表**: titles
- **过滤条件**: title_type = 'movie', start_year >= 2020
- **验证方法**: 单行单列结果，数字应合理（几万到几十万级别）
- **测试结果**
  - 测试报告D:\code_work_space\llm\nl2sql\src\agent\workspace\report\2020年后电影数量统计_2026-07-31_10-36-57.md

### Q2_2：查询 2020 年之后上映的电视剧数量?

**测试结果**

- 测试报告D:\code_work_space\llm\nl2sql\src\agent\workspace\report\2020年后电视剧数量统计_2026-07-31_10-41-00.md

### Q3：查询汤姆·汉克斯（Tom Hanks）的出生年份
- **问题**: Tom Hanks 的出生年份是哪一年？

- **涉及表**: names

- **过滤条件**: primary_name = 'Tom Hanks'

- **验证方法**: 应为 1956

- **测试结果**

  ```sql
  SQL：SELECT birth_year FROM names WHERE primary_name = 'Tom Hanks'
  策略：策略B（快速通道）— 单表简单筛选查询
  结果：Tom Hanks 出生于 1956 年 🎂
  ```

### Q4：查询类型（genre）为 "Comedy" 的电影数量
- **问题**: Comedy 类型的电影有多少部？

- **涉及表**: titles_genres, genres, titles

- **过滤条件**: genres.display_name = 'Comedy', titles.title_type = 'movie'

- **验证方法**: 单行单列，结果应为几十万级别

- **测试结果**

  ```sql
  SELECT COUNT(DISTINCT t.id) AS comedy_movie_count
  FROM titles t
  JOIN titles_genres tg ON t.id = tg.title_id
  JOIN genres g ON tg.genre_id = g.id
  WHERE t.title_type = 'movie'
    AND g.id = 'comedy'
   
  Comedy 类型的电影共有 66,885 部。
  ```

  

### Q5：查询片长（runtime）超过 180 分钟的电影
- **问题**: 列出 时长 大于 180 的电影标题和片长，按片长降序排列

- **涉及表**: titles

- **过滤条件**: title_type = 'movie', runtime_minutes > 180  业务规则中描述的是片长

- **验证方法**: 返回结果中 runtime 均 > 180

- **测试结果**

  测试报告D:\code_work_space\llm\nl2sql\src\agent\workspace\report\时长大于180分钟的电影_2026-07-31_11-00-49.md

  

### Q6：查询电视剧系列（tvSeries）的总数
- **问题**: 数据库中有多少部电视剧系列？

- **涉及表**: titles

- **过滤条件**: title_type = 'tvSeries'

- **验证方法**: 单行单列，结果应为几万级别

- **测试结果**

  ```sql
   SELECT COUNT(*) FROM titles WHERE title_type = 'tvSeries'
   数据库中共有 232,074 部电视剧系列（title_type = 'tvSeries'）
  ```

  

### Q7：查询 1990 年代（1990-1999）电影的平均评分
- **问题**: 1990 年代上映的电影平均评分是多少？

- **涉及表**: titles

- **过滤条件**: title_type = 'movie', start_year BETWEEN 1990 AND 1999

- **验证方法**: 结果应在 6.0-7.0 之间

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\1990s_Movies_Avg_Rating_2026-07-31_11-21-47.md

  


### Q8：查询投票数（num_votes）超过 100 万的电影数量
- **问题**: 有多少部电影的投票数超过 100 万？

- **涉及表**: titles

- **过滤条件**: num_votes > 1000000

- **验证方法**: 单行单列，结果应为几百到几千级别

- **测试结果**

  ```sql
  SELECT COUNT(*) AS movie_count
  FROM titles
  WHERE title_type = 'movie' AND num_votes > 1000000
  
  投票数超过 100 万的电影共有 22 部。
  ```

  

---

## 二、⭐⭐ 中等问题（8题）

### Q9：查询每个类型的电影数量，按数量降序排列
- **问题**: 统计每个类型的电影数量，从多到少排列

- **涉及表**: titles_genres, genres, titles

- **关联**: titles_genres.genre_id = genres.id, titles_genres.title_id = titles.id

- **过滤条件**: titles.title_type = 'movie'

- **验证方法**: Drama 类型应排第一，数量最多

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\各类型电影数量分布_2026-07-31_11-32-19.md

### Q10：查询参演电影最多的前 10 位演员
- **问题**: 参演电影数量最多的前 10 位演员是谁？显示姓名和参演数量
- **涉及表**: principals, names, titles
- **关联**: principals.name_id = names.id, principals.title_id = titles.id
- **过滤条件**: category_id IN ('actor', 'actress'), title_type = 'movie'
- **验证方法**: 返回 10 条记录，参演数量从高到低
- **测试结果**

### Q11：查询克里斯托弗·诺兰（Christopher Nolan）导演的电影平均评分
- **问题**: Christopher Nolan 导演的电影平均评分是多少？

- **涉及表**: principals, names, titles

- **关联**: principals.name_id = names.id, principals.title_id = titles.id

- **过滤条件**: names.primary_name = 'Christopher Nolan', category_id = 'director'

- **验证方法**: 结果应在 7.5-8.5 之间

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\Christopher_Nolan_Avg_Rating_2026-07-31_12-24-30.md



### Q12：查询评分最高的电视剧系列（tvSeries）前 10 名
- **问题**: 评分最高的 10 部电视剧系列是哪些？显示标题和评分
- **涉及表**: titles
- **过滤条件**: title_type = 'tvSeries'
- **验证方法**: 返回 10 条记录，评分从高到低

### Q13：查询每一年电视剧的平均评分趋势（2000-2023）
- **问题**: 2000 年到 2023 年每年电视剧的平均评分是多少？按年份排列

- **涉及表**: titles

- **过滤条件**: title_type = 'tvSeries', start_year BETWEEN 2000 AND 2023

- **验证方法**: 返回 24 行（每年一行），评分应在 5.5-7.5 之间波动

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\电视剧平均评分趋势_2026-07-31_12-27-52.md

### Q14：查询参演电影平均评分最高的前 10 位演员（投票数 > 50000）
- **问题**: 参演电影平均评分最高的前 10 位演员是谁？要求参演电影数 >= 5 且每部电影投票数 > 50000

- **涉及表**: principals, names, titles

- **关联**: 三表 JOIN

- **过滤条件**: category_id IN ('actor', 'actress'), num_votes > 50000

- **验证方法**: 返回 10 条记录，评分从高到低

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\Top10_Actors_by_Avg_Rating_2026-07-31_13-41-42.md

### Q15：查询 "The Matrix" 的所有参演演员
- **问题**: 电影 "The Matrix" 有哪些演员参演？显示演员姓名和角色排序

- **涉及表**: titles, principals, names

- **关联**: titles.id = principals.title_id, principals.name_id = names.id

- **过滤条件**: titles.primary_title = 'The Matrix', category_id IN ('actor', 'actress')

- **验证方法**: 应包含 Keanu Reeves, Laurence Fishburne 等

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\The_Matrix_演员查询报告_2026-07-31_15-07-00.md

### Q16：查询每个年代（decade）电影的平均评分和电影数量
- **问题**: 按十年分组统计电影的平均评分和数量
- **涉及表**: titles
- **过滤条件**: title_type = 'movie', start_year IS NOT NULL
- **提示**: 使用 FLOOR(start_year / 10) * 10 计算年代
- **验证方法**: 结果按年代排列，早期年代数量少，近期数量多
- **测试结果**

---

## 三、⭐⭐⭐ 较难问题（8题）

### Q17：查询既是演员又是导演的跨界者，对比其演员作品和导演作品的平均评分
- **问题**: 找出同时是演员和导演的人，分别计算他们作为演员和作为导演的作品平均评分，要求两种角色参演作品数均 >= 3

- **涉及表**: principals, names, titles

- **关联**: 多表 JOIN，需要分别聚合演员和导演作品

- **验证方法**: 结果应包含知名跨界者如 Clint Eastwood, Ben Affleck 等

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\演员导演双角色平均评分_2026-07-31_13-47-17.md

### Q18：查询电视剧系列评分衰退最严重的 Top 10
- **问题**: 找出评分衰退最严重的电视剧系列，计算首季平均评分和末季平均评分的下降幅度

- **涉及表**: titles, episodes

- **关联**: titles.id = episodes.parent_id（系列）, episodes.id = titles.id（剧集）

- **过滤条件**: title_type = 'tvSeries', 至少 3 季

- **验证方法**: 结果应包含评分下降明显的系列

- **测试结果**

  测试报告 找回历史sql

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\TV_Series_Rating_Decline_2026-07-31_13-34-15.md

### Q19：查询 "宝藏片"（评分高但投票数少）Top 20
- **问题**: 找出评分 >= 8.5 但投票数 < 5000 的电影，按评分降序排列

- **涉及表**: titles

- **过滤条件**: title_type = 'movie', average_rating >= 8.5, num_votes < 5000

- **验证方法**: 返回 20 条记录，这些是冷门高分电影

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\Hidden_Gems_Movies_2026-07-31_13-51-56.md

### Q20：查询导演-演员黄金搭档（合作 3 次以上且平均评分 >= 7.5）
- **问题**: 找出合作 3 次以上的导演-演员组合，且他们合作电影的平均评分 >= 7.5

- **涉及表**: principals（两次 JOIN，一次作为导演，一次作为演员）, names, titles

- **关联**: 复杂自关联

- **验证方法**: 应包含知名黄金搭档如 Nolan/DiCaprio, Scorsese/De Niro 等

- **测试结果**

  测试报告

  D:\code_work_space\llm\nl2sql\src\agent\workspace\report\Director_Actor_Collaborations_2026-07-31_14-25-38.md

### Q21：查询每个类型中评分最高的电影
- **问题**: 每个类型中评分最高的电影是哪部？显示类型名、电影标题和评分
- **涉及表**: titles_genres, genres, titles
- **关联**: 三表 JOIN + 窗口函数（ROW_NUMBER）
- **验证方法**: 每个类型返回一条记录，约 28 行
- **测试结果**

### Q22：查询职业生涯跨度最长的前 10 位演员
- **问题**: 职业生涯跨度（从第一部到最后一部作品的年数）最长的前 10 位演员
- **涉及表**: principals, names, titles
- **关联**: 三表 JOIN，计算 MAX(start_year) - MIN(start_year)
- **过滤条件**: category_id IN ('actor', 'actress')
- **验证方法**: 返回 10 条记录，跨度从高到低
- **测试结果**

### Q23：查询每年电影产量的变化趋势（2000-2023），并计算同比增长率
- **问题**: 统计 2000-2023 年每年电影产量，并计算相比上一年的增长率
- **涉及表**: titles
- **过滤条件**: title_type = 'movie', start_year BETWEEN 2000 AND 2023
- **提示**: 使用 LAG 窗口函数计算同比增长率
- **验证方法**: 结果包含年份、数量、增长率三列
- **测试结果**

### Q24：查询参演了最多不同类型（genre）电影的演员 Top 10
- **问题**: 参演了最多不同类型电影的演员 Top 10，显示演员名和涉及的类型数
- **涉及表**: principals, names, titles_genres, genres
- **关联**: 四表 JOIN
- **过滤条件**: category_id IN ('actor', 'actress')
- **验证方法**: 返回 10 条记录，类型数从高到低
- **测试结果**

---

## 四、⭐⭐⭐⭐ 困难问题（6题）

### Q25：使用贝叶斯校正评分计算 Top 20 电影
- **问题**: 使用贝叶斯加权评分（Bayesian Adjusted Rating）重新计算电影评分，选出 Top 20 电影。公式：(num_votes × average_rating + C × global_avg) / (num_votes + C)，其中 C = 1000
- **涉及表**: titles
- **过滤条件**: title_type = 'movie', num_votes > 0
- **验证方法**: 与普通评分排序对比，低投票数电影排名会下降
- **测试结果**

### Q26：查询每个年代最赚钱的类型（类型 ROI 分析）
- **问题**: 按年代和类型分组，计算类型 ROI = (类型平均评分 - 整体平均评分) / 整体平均评分 × (类型平均投票数 / 整体平均投票数)，找出每个年代 ROI 最高的类型
- **涉及表**: titles, titles_genres, genres
- **关联**: 多表 JOIN + 窗口函数
- **过滤条件**: title_type = 'movie'
- **验证方法**: 每个年代返回一个类型，早期年代和近期年代的类型可能不同
- **测试结果**

### Q27：查询演员的 "Star Power Index" Top 10
- **问题**: 计算演员的 Star Power Index = AVG(参演电影评分) × LOG(SUM(参演电影投票数)) × (参演电影数 / 职业生涯跨度)，选出 Top 10
- **涉及表**: principals, names, titles
- **关联**: 三表 JOIN + 复杂计算
- **过滤条件**: category_id IN ('actor', 'actress'), 参演电影数 >= 10
- **验证方法**: 返回 10 条记录，应为全球顶级演员
- **测试结果**

### Q28：查询电视剧系列的评分趋势分类
- **问题**: 对电视剧系列按评分趋势分类：严重衰退（衰退率 > 30%）、明显下滑（15-30%）、稳定（±15%）、逆势上升（> 15%），统计每类有多少个系列
- **涉及表**: titles, episodes
- **关联**: 多表 JOIN + 窗口函数 + CASE WHEN
- **过滤条件**: title_type = 'tvSeries', 至少 3 季
- **验证方法**: 4 行结果，"稳定"类应占多数
- **测试结果**

### Q29：查询 "Hidden Gem Score" 最高的电影 Top 20
- **问题**: Hidden Gem Score = average_rating / LOG10(num_votes + 1)，找出被低估的高分冷门电影 Top 20
- **涉及表**: titles
- **过滤条件**: title_type = 'movie', num_votes >= 100（排除极端低投票数）
- **验证方法**: 结果应为评分较高但投票数相对较少的电影
- **测试结果**

### Q30：查询多维度交叉分析 — 每个年代-类型组合的电影数量、平均评分、平均投票数
- **问题**: 按年代和类型交叉分组，统计每个组合的电影数量、平均评分、平均投票数，找出每个年代中电影数量最多的类型
- **涉及表**: titles, titles_genres, genres
- **关联**: 三表 JOIN + 窗口函数（ROW_NUMBER）
- **过滤条件**: title_type = 'movie'
- **验证方法**: 每个年代返回一个类型，结果应展示类型流行度的年代变迁
- **测试结果**

---

## 附录：验证参考数据

### 已知事实（可用于验证查询准确性）

| 验证项 | 预期值 | 对应问题 |
|--------|--------|:--------:|
| Tom Hanks 出生年份 | 1956 | Q3 |
| Christopher Nolan 导演作品平均评分 | ~8.2 | Q11 |
| "The Matrix" 主演 | Keanu Reeves, Laurence Fishburne 等 | Q15 |
| Drama 类型电影数量最多 | 排名第一 | Q9 |
| 电影总数量（title_type='movie'） | 约 60-70 万 | Q2 |
| 电视剧系列数量（title_type='tvSeries'） | 约 5-8 万 | Q6 |
| 1990s 电影平均评分 | 约 6.2-6.5 | Q7 |

### 验证技巧

1. **简单问题**: 结果应直观可理解，数量级合理
2. **JOIN 问题**: 检查 JOIN 条件是否正确，避免笛卡尔积
3. **聚合问题**: 检查 GROUP BY 字段是否完整，避免数据重复
4. **窗口函数**: 检查 PARTITION BY 和 ORDER BY 是否正确
5. **复杂计算**: 分步验证，先验证中间结果再验证最终结果
6. **NULL 处理**: 注意 start_year、runtime_minutes 等字段可能为 NULL
