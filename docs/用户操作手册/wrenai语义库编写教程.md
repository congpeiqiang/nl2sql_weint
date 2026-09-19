---
title: Wrenai 语义库编写教程 —— 逐层逐字段（以 witops 语义库为样例）
type: tutorial
audience: [语义库编写人员, 数据建模工程师, 新入职工程师]
runs: yes
verified_on: 2026-09-19
sources:
  - D:\code_work_space\llm\witops-wrenai\wren_project.yml
  - D:\code_work_space\llm\witops-wrenai\relationships.yml
  - D:\code_work_space\llm\witops-wrenai\models\do_bug\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\models\do_department_user_detail\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\views\v_workhour\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\views\v_workhour\sql.yml
  - D:\code_work_space\llm\witops-wrenai\views\v_story\sql.yml
  - D:\code_work_space\llm\witops-wrenai\views\v_bug\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\cubes\workhour_analysis\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\cubes\story_delivery\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\cubes\bug_quality\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\knowledge\knowledge.yml
  - D:\code_work_space\llm\witops-wrenai\knowledge\sql\月度工时统计.md
  - D:\code_work_space\llm\witops-wrenai\knowledge\caveats\常见陷阱.md
  - D:\code_work_space\llm\witops-wrenai\target\mdl.json
  - D:\code_work_space\llm\nl2sql\.venv\Lib\site-packages\wren\context.py
  - D:\code_work_space\llm\nl2sql\.venv\Lib\site-packages\wren\context_cli.py
  - D:\code_work_space\llm\nl2sql\src\agent\shared\skills\nl2sql\sql-of-thought\scripts\gen_models_mysql.py
---

# Wrenai 语义库编写教程 —— 逐层逐字段

样例仓库：`D:\code_work_space\llm\witops-wrenai`（公司运营管理平台语义库）。
本文所有字段、类型、样例值都取自该仓库的真实文件与构建产物 `target/mdl.json`，命令均已实跑。

**你要写的只有三层**：`views/`、`cubes/`、`knowledge/`。
`models/` 由代码从数据库表结构自动生成，不要手改；但你必须看得懂它，因为它决定了你能引用哪些表和字段。

---

## 导读：Wrenai 语义库是什么，能完成什么

### 一句话定义

**Wrenai 语义库是一个「业务语义层」项目**：用一组可版本管理的 YAML / Markdown 文件，把物理数据库翻译成业务人员与 AI 都能理解的概念——表、字段、指标、维度、口径、样例。
它不搬数据、不建表，只在**查询时**把「业务问法」编译成目标数据库能执行的 SQL。

本平台用的引擎版本：`wrenai 0.13.0`（内核 `wren_core 0.7.1`），命令行工具 `wren`，项目定义文件在 `witops-wrenai/`。

### 它解决什么问题

数据库本身只有表名和编码，直接让 AI 或新人写 SQL，会稳定地踩同几个坑：

| 现实问题 | 语义库给出的答案 |
| --- | --- |
| 151 张表，只有 48 张该用；用错表结果就错 | 只有 `models/` 里建模过的表可被引用，**越界直接规划失败** |
| 软删字段 `deleted`，`do_work_hour_detail_extend` 493 万行里 96% 是废数据 | 视图里固化 `WHERE deleted = 0`，使用者不必记得 |
| `status = 14` 是什么？新字典和旧流程码混在一张表里 | 视图用 `CASE` 就地映射中文名，术语表登记全部编码 |
| 同一个「完成率」每个人分母取得不一样 | 指标写进立方体，分母写进 `description`，口径唯一 |
| AI 每次都要重新拼 SQL，风格与正确性都不稳定 | 立方体查询按「指标 + 维度」提问，样例沉淀成 `knowledge/sql` |
| 改了口径，散落各处的 SQL 不知道有没有跟上 | 口径集中在 `views/` + `knowledge/`，可 diff、可校验、可回归 |

### 能力清单

| 能力 | 说明 | 本库实例 |
| --- | --- | --- |
| 语义建模 | 声明物理表、字段、类型、中文描述 | 48 个模型（`models/`） |
| 视图封装 | 用原生 SQL 固化过滤、枚举映射、JOIN、派生维度 | 5 个视图（`v_story` / `v_bug` / `v_iteration_burndown` / `v_project` / `v_workhour`） |
| 指标与维度 | 按「指标 + 维度」提问，引擎生成 SQL | 6 个立方体；指标定义 88 条、维度定义 96 条（去重后 81 个指标、65 个维度） |
| 时间分析 | 时间维度支持年/季/月/周/日粒度下钻，区间左闭右开 | 21 个时间维度，统一 `levels: [year, quarter, month, week, day]` |
| 层级钻取 | 声明「项目集 → 项目 → 模块」这类层级 | `bug_quality`、`story_delivery` 的「项目归属」 |
| 物理 SQL 编译 | 把视图展开成带 CTE 的真实 SQL，并按方言输出 | `wren dry-plan` 展开 `v_workhour` → `do_work_hour_detail_extend` |
| 校验与护栏 | 三档校验（结构 / 描述 / 列描述）+ 规划期检查 | `wren context validate --level strict` |
| 业务知识注入 | 规则、术语、指标、陷阱以 Markdown 供给 AI | `knowledge/` 五个目录，19 个文件 |
| 历史取数复用 | 已验证的 NL→SQL 样例做语义召回，相似问题直接复用 | 12 条样例，索引 1116 条 schema 项 + 144 条种子查询 |
| 对外工具化 | 通过 MCP 暴露查询与知识工具，供 AI 子智能体调用 | 19 个工具（见下） |
| 多方言支持 | 同一套建模可切 MySQL / PostgreSQL / ClickHouse / DuckDB 等 | 本库为 `mysql` |

### 在平台里它怎么被用上

平台对数据库有两条通道，语义库是其中「语义层通道」的全部底座：

```text
用户中文提问
  → 子智能体（nl2sql_agent）
     ├─ 语义层通道：MCP wrenai_<库名>  → 立方体/视图 → 编译物理 SQL → MySQL   ← 语义库在这里生效
     └─ 直连通道：  MCP dbmcp          → 直接执行 SQL                        ← 不受语义库约束
  → 结果 + 图表 + 报告
```

子智能体拿到的 wren MCP 工具（`wren serve mcp`，本平台以 stdio 子进程方式装载，**不开放写工具**，因此是只读接入）：

| 分组 | 工具 |
| --- | --- |
| 查询执行 | `run_sql`、`query_cube`、`dry_run`、`dry_plan` |
| 元数据 | `get_mdl`、`list_models`、`describe_model`、`list_cubes`、`describe_cube`、`get_data_source`、`list_functions`、`describe_schema` |
| 知识 | `get_instructions`、`get_all_knowledge`、`list_knowledge`、`get_context`、`recall_queries`、`list_stored_queries` |
| 写入（默认关闭） | `store_query`（需 `--allow-write`，本平台未开启） |

除工具外还提供只读资源：`wren://mdl`、`wren://instructions`、`wren://project`、`wren://agents`、`wren://knowledge/{name}`、`wren://knowledge/{子目录}/{文件名}`。

### 它不做什么（边界）

- **不做 ETL、不落数据、不改表结构**：语义库里没有一行业务数据，只有定义；全部查询走只读账号。
- **不替代报表与 BI**：它解决的是「取数口径与自然语言取数」，不是图表排版与权限门户。
- **不做权限控制**：能查什么取决于建模了什么、数据库账号被授了什么权。
- **不自动理解业务**：口径要人写。写得含糊，AI 就答得含糊——这正是本教程要解决的问题。

### 本库现状（2026-09-19 实测）

```text
48 个模型 · 5 个视图 · 6 个立方体 · 0 条关系
指标定义 88 条 · 维度定义 96 条 · 时间维度 21 条 · 隐藏列 14 个
knowledge/：19 个文件 = 3 条规则 + 术语表 + 指标定义 + 陷阱 + 12 条 NL→SQL 样例
```

---

## 0. 文件地图

| 文件 | 谁写 | 作用 | 引擎/后端怎么用 |
| --- | --- | --- | --- |
| `wren_project.yml` | 管理员 | 项目元信息 | 定位项目、确定数据源与连接 |
| `models/<表>/metadata.yml` | 代码生成 | 物理表结构 | 告诉引擎表、列、类型、描述 |
| `views/<视图>/metadata.yml` | **你写** | 视图声明 | 视图名、方言、说明 |
| `views/<视图>/sql.yml` | **你写** | 视图 SQL | 软删过滤、枚举映射、JOIN 固化 |
| `cubes/<立方体>/metadata.yml` | **你写** | 指标与维度 | AI 按「指标 + 维度」提问 |
| `knowledge/knowledge.yml` | 可选 | 知识库版本 | 版本轴声明 |
| `knowledge/rules/*.md` | **你写** | 强制口径 | `wren context instructions`、后端 |
| `knowledge/glossary/*.md` | **你写** | 术语与枚举 | MCP 知识工具 |
| `knowledge/metrics/*.md` | **你写** | 指标定义 | MCP 知识工具 |
| `knowledge/caveats/*.md` | **你写** | 陷阱与反例 | MCP 知识工具 |
| `knowledge/sql/*.md` | **你写/工具生成** | 已验证 NL→SQL | `wren memory recall`、后端记忆 |
| `relationships.yml` | 管理员 | 表间关系 | 声明 JOIN 路径（本库为空） |
| `target/mdl.json` | 工具生成 | 编译产物 | **引擎真正读取的文件**，需入库 |

样例仓库现状：48 个模型、5 个视图、6 个立方体、0 条关系。

三层之间的引用关系是单向的：

```text
物理表(MySQL) → models(自动) → views(你写) → cubes(你写)
                                   ↘ knowledge(你写，给 AI 补充口径)
```

立方体只能绑一个视图，视图只能引用物理表（不能引用别的视图）。所有关联都在视图里做完。

---

## 1. 第一层：views —— 把物理表翻译成业务视图

一个视图 = 一个目录 + 两个文件。

```text
views/v_workhour/
├─ metadata.yml     视图声明
└─ sql.yml          视图 SQL
```

样例：`views/v_workhour/`（工时投入视图，全库最简单的一个）；复杂样例见 `views/v_story/`。

### 1.1 `views/<视图名>/metadata.yml` 字段

| 字段 | YAML 类型 | 必填 | 含义 | 本库样例 |
| --- | --- | --- | --- | --- |
| `name` | 字符串 | 是 | 视图名。**全局唯一**：不能与任何模型或其他视图重名 | `v_workhour` |
| `dialect` | 字符串 | 否 | 视图 SQL 的方言，取值 `mysql` / `postgres` / `duckdb` / `bigquery` / `snowflake` / `clickhouse` 等 | `mysql` |
| `properties.description` | 字符串 | 强烈建议 | 给 AI 看的说明：**粒度 + 过滤条件 + 主要维度** | `"工时投入视图（已过滤 deleted=0）— 含人员/部门/项目/月份等分析维度"` |

真实样例（`views/v_workhour/metadata.yml`，含工程注释）：

```yaml
# 工时投入视图（schema_version 5 —— 原生 SQL 视图）
# 为什么要建视图：源表 do_work_hour_detail_extend 共 493 万行，其中 deleted=0 仅 19.6 万行（96% 为软删记录）。
# cube 直接基于源表会得出严重错误的工时；视图负责过滤并派生常用维度。
name: v_workhour
dialect: mysql
properties:
  description: "工时投入视图（已过滤 deleted=0）— 含人员/部门/项目/月份等分析维度"
```

要点：

- **`name` 不要用中文**，用 `v_` + 业务域小写下划线：`v_story`、`v_bug`、`v_iteration_burndown`、`v_project`、`v_workhour`。
- 没有 `columns` 字段：视图的列由 `statement` 的 `SELECT` 决定，引擎在执行期推导类型。
- 视图**不放大行数**是设计底线：多对多关联用 `COUNT(DISTINCT ...)` 预聚合成列带回。

### 1.2 `views/<视图名>/sql.yml` 字段

| 字段 | YAML 类型 | 必填 | 含义 | 本库样例 |
| --- | --- | --- | --- | --- |
| `statement` | 多行字符串（`\|` 块） | 是 | 视图的完整 SELECT 语句 | 见下 |

`statement` 也可以直接写在 `metadata.yml` 里（单行时引擎会自动这样处理），但**生产约定：SQL 一律放 `sql.yml`**，便于阅读与 diff。

真实样例（`views/v_workhour/sql.yml`，节选）：

```yaml
statement: |
  SELECT
    id,
    user_id,
    user_name,
    job_number,
    department_full_name,
    SUBSTRING_INDEX(department_full_name, '/', 1) AS dept_level1,
    date AS work_date,
    DATE_FORMAT(date, '%Y-%m') AS work_month,
    project_id,
    project_name,
    ...
    work_hour
  FROM do_work_hour_detail_extend
  WHERE deleted = 0
```

这个视图做了三件事：过滤软删（`WHERE deleted = 0`）、改列名（`date AS work_date`）、派生新维度（`dept_level1`、`work_month`）。

### 1.3 视图 SQL 的六条硬规则（全部实测）

| 规则 | 违反后的报错 | 正确写法 |
| --- | --- | --- |
| 非聚合列必须出现在 GROUP BY | `Column in SELECT must be in GROUP BY or an aggregate function: ... column "x" must appear in the GROUP BY clause` | 用 `MAX(x)` 包裹，再 `GROUP BY` 主键 |
| 不能用 `DATE()` | `Invalid function 'date'. Did you mean 'atan'?` | `CAST(x AS DATE)` |
| 不能用 `GROUP_CONCAT()` | `Invalid function 'group_concat'. Did you mean 'array_concat'?` | 名单类字段不放视图，需要时直查关联表 |
| 不能引用其他视图 | `table 'wren.public.v_workhour' not found` | 只能 `FROM` 物理表；公共逻辑各自重写 |
| 派生表（`FROM (SELECT ...)`）风险高 | 规划期可能报 `No field named ...` | 用扁平 `JOIN`，本库 5 个视图全部如此 |
| 每张 JOIN 表都要 `deleted = 0`，且条件写在 `ON` 里 | 结果虚高或 `LEFT JOIN` 退化成内连接 | `LEFT JOIN t ON t.id = x.tid AND t.deleted = 0` |

另外：`wren context validate` 通过 ≠ 视图能规划。以上多数限制在 `context build` 之后的**规划期**才暴露，所以每次改完视图都要按 1.5 验证。

### 1.4 三种典型视图写法（都来自本库）

**写法 A：过滤 + 派生维度**（`views/v_workhour/sql.yml`）
适合「源表脏、需要统一入口」的场景：

```sql
SELECT
  id, user_id, user_name, job_number, department_full_name,
  SUBSTRING_INDEX(department_full_name, '/', 1) AS dept_level1,
  date AS work_date,
  DATE_FORMAT(date, '%Y-%m') AS work_month,
  project_id, project_name, work_hour
FROM do_work_hour_detail_extend
WHERE deleted = 0
```

**写法 B：枚举就地映射为中文名**（`views/v_story/sql.yml`）
同时保留原始码与中文名，AI 既能按名提问，也能按码过滤：

```sql
    MAX(s.status)               AS status_code,
    MAX(COALESCE(CASE s.status
      WHEN 0  THEN '草稿中'   WHEN 1  THEN '待接收'   WHEN 2  THEN '待设计'
      WHEN 5  THEN '开发阶段' WHEN 14 THEN '已完成'   WHEN 6  THEN '已取消'
      WHEN 99 THEN '关闭'
      -- 其余状态码（3/4/9/10/11/12/13/15/97 等）在真实文件中同样逐条列出，此处省略
      ELSE CONCAT('其他(', s.status, ')')
    END, '未设置'))             AS status_name,
```

**写法 C：一对多预聚合，保证一行一主体**（`views/v_story/sql.yml`）
一个需求可能挂在多个项目下，先 `COUNT(DISTINCT)` 聚合再带回，行数不被放大：

```sql
    COUNT(DISTINCT sp.project_id) AS app_project_count,
    COUNT(DISTINCT r.id)          AS dep_story_count
  FROM do_story s
  LEFT JOIN do_story_project sp ON sp.story_id = s.id AND sp.deleted = 0
  LEFT JOIN do_story_rel r      ON r.story_id = s.id AND r.deleted = 0
  WHERE s.deleted = 0
  GROUP BY s.id
```

### 1.5 写完怎么验证视图

```powershell
$wren = "D:\code_work_space\llm\nl2sql\.venv\Scripts\wren.exe"
$env:PYTHONUTF8 = '1'
cd D:\code_work_space\llm\witops-wrenai

& $wren context validate      # 结构校验：Valid — 48 models, 5 views, 0 relationships.
& $wren context build         # 编译：Built: 48 models, 5 views → target/mdl.json
& $wren --sql "SELECT * FROM v_workhour LIMIT 5" --output json   # 真查一次
```

只看规划不连库（最快）：

```powershell
& $wren dry-plan --sql "SELECT dept_level1, SUM(work_hour) FROM v_workhour GROUP BY 1" --datasource mysql
```

---

## 2. 第二层：cubes —— 把视图变成指标与维度

一个立方体 = 一个目录 + 一个文件：`cubes/<立方体名>/metadata.yml`。

样例：`cubes/workhour_analysis/`（最小可读）；`cubes/story_delivery/`（23 指标 + 23 维度）；`cubes/bug_quality/`、`cubes/iteration_burndown_cube/`（带层级）。

### 2.1 顶层字段

| 字段 | YAML 类型 | 必填 | 含义 | 本库样例 |
| --- | --- | --- | --- | --- |
| `name` | 字符串 | 是 | 立方体名，全局唯一 | `workhour_analysis` |
| `description` | 字符串 | 强烈建议 | 一句话说明「能回答什么问题」 | `"工时投入分析立方体 — 按人员/部门/项目/产品/月份维度下钻..."` |
| `base_object` | 字符串 | 是 | 绑定的**模型或视图名**（本库全部绑视图） | `v_workhour` |
| `measures` | 列表 | 是（空会告警 `cube has no measures`） | 指标 | 见 2.2 |
| `dimensions` | 列表 | 否 | 普通维度 | 见 2.3 |
| `time_dimensions` | 列表 | 否 | 时间维度 | 见 2.4 |
| `hierarchies` | 映射 | 否 | 钻取层级：`层级名: [维度1, 维度2, ...]` | 见 2.5 |

立方体里**不能写 SQL、不能写 JOIN**。`expression` 里只能引用 `base_object` 的列，或对它们做聚合与运算。

### 2.2 `measures[]`（指标）字段

| 字段 | YAML 类型 | 必填 | 含义 | 取值 |
| --- | --- | --- | --- | --- |
| `name` | 字符串 | 是 | 指标名，**在立方体内唯一**；不要与它聚合的列同名 | 小写下划线，如 `total_hours`、`done_rate` |
| `expression` | 字符串 | 是 | 聚合表达式 | SQL 聚合：`SUM(...)`、`COUNT(*)`、`COUNT(DISTINCT ...)`、`AVG(...)`、`SUM(CASE WHEN ... THEN 1 ELSE 0 END)` |
| `type` | 字符串 | 是 | 结果类型 | 本库只用 `BIGINT`（计数）与 `DOUBLE`（金额/工时/比率/均值）；实际分布：BIGINT 59 个、DOUBLE 29 个 |
| `description` | 字符串 | 强烈建议 | 口径说明：**分母是什么、排除了什么、单位是什么** | `"需求完成率（已完成 / 排除已取消后的需求数）"` |

真实样例（`cubes/workhour_analysis/metadata.yml`）：

```yaml
measures:
  - name: total_hours
    expression: "SUM(work_hour)"
    type: DOUBLE
    description: "工时合计（小时）"

  - name: user_count
    expression: "COUNT(DISTINCT user_id)"
    type: BIGINT
    description: "报工人数"

  - name: avg_hours_per_user
    expression: "SUM(work_hour) / NULLIF(COUNT(DISTINCT user_id), 0)"
    type: DOUBLE
    description: "人均工时（小时）"

  - name: approve_rate
    expression: "SUM(CASE WHEN if_approve = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0)"
    type: DOUBLE
    description: "工时审核通过率（已审核记录 / 总记录）"
```

写指标的三个约定：

1. **除法一律 `NULLIF(分母, 0)`**，否则除零会得到 NULL 或报错。
2. **条件计数**写成 `SUM(CASE WHEN 条件 THEN 1 ELSE 0 END)`，而不是靠外部过滤。
3. **比率要在描述里写清分母**：本库 `done_rate` 的分母是「排除已取消后的需求数」，不写清楚 AI 会算错。

### 2.3 `dimensions[]`（维度）字段

| 字段 | YAML 类型 | 必填 | 含义 | 取值 |
| --- | --- | --- | --- | --- |
| `name` | 字符串 | 是 | 维度名，建议与视图列同名 | `department_full_name`、`status_name` |
| `expression` | 字符串 | 是 | 取值表达式，通常是视图列名；也可以是轻量函数 | `dept_level1`、`SUBSTRING_INDEX(department_full_name, '/', 1)` |
| `type` | 字符串 | 是 | 数据类型 | 本库只用 `string`（68 个）与 `int`（28 个） |
| `description` | 字符串 | 强烈建议 | 该维度是什么、编码含义 | `"是否已审核（0=未审核，1=已审核）"` |

真实样例：

```yaml
dimensions:
  - name: dept_level1
    expression: "SUBSTRING_INDEX(department_full_name, '/', 1)"
    type: string
    description: "一级部门"

  - name: if_approve
    expression: "if_approve"
    type: int
    description: "是否已审核（0=未审核，1=已审核）"
```

约定：**保留原始编码维度**（`status_code`、`if_approve`），同时提供中文名维度（`status_name`），编码维度便于精确过滤，名称维度便于分组展示。

### 2.4 `time_dimensions[]`（时间维度）字段

| 字段 | YAML 类型 | 必填 | 含义 | 取值 |
| --- | --- | --- | --- | --- |
| `name` | 字符串 | 是 | 时间维度名 | `work_date`、`complete_time`、`mention_time` |
| `expression` | 字符串 | 是 | 时间列 | 视图里的日期/时间列 |
| `type` | 字符串 | 是 | 类型 | 本库全部为 `date`（21 个） |
| `description` | 字符串 | 强烈建议 | 该时间的业务含义 | `"报工日期"` |
| `levels` | 列表 | 否 | 允许的下钻粒度 | 本库统一为 `[year, quarter, month, week, day]` |

真实样例：

```yaml
time_dimensions:
  - name: work_date
    expression: "work_date"
    type: date
    description: "报工日期"
    levels: [year, quarter, month, week, day]
```

**查询时的时间区间是左闭右开** `[start, end)`：

```powershell
& $wren cube query --cube workhour_analysis --measures total_hours --time-dimension work_date:month:2026-01-01,2026-04-01 --sql-only
```

生成的 SQL 是：

```sql
SELECT DATE_TRUNC('month', work_date) AS work_date__month, SUM(work_hour) AS total_hours
FROM v_workhour
WHERE work_date >= '2026-01-01' AND work_date < '2026-04-01'
GROUP BY 1 ORDER BY 1
```

要一整季度就写到**下个季度的第一天**；写成 `2026-03-31` 会静默漏掉 3 月 31 日（实测少算约 1400 小时）。

### 2.5 `hierarchies`（钻取层级）字段

结构是「层级名 → 有序维度名列表」，层级名可以用中文，供展示与钻取：

```yaml
hierarchies:
  项目归属:
    - project_collect_name
    - project_name
    - project_module_name
```

本库有 2 个立方体用了它（`bug_quality`、`story_delivery`）。列表里的每个名字**必须是已声明的 `dimensions` 或 `time_dimensions`**，否则报 `hierarchies.x references unknown dimension 'y'`。

### 2.6 怎么写立方体查询

| 参数 | 格式 | 说明 |
| --- | --- | --- |
| `--cube` | `--cube workhour_analysis` | 立方体名 |
| `--measures` | `--measures total_hours,user_count` | 逗号分隔 |
| `--dimensions` | `--dimensions dept_level1` | 逗号分隔 |
| `--time-dimension` | `--time-dimension work_date:month:2026-01-01,2026-04-01` | `名:粒度[:起,止]`，区间左闭右开 |
| `--filter` | `--filter if_approve:eq:1` | `维度:运算符[:值]`，可重复 |
| `--limit` / `--offset` | `--limit 10` | 行数控制 |
| `--sql-only` | — | 只打印 SQL，不连库 |
| `--output` | `--output json` | **核对数据一律用 json**（table/csv 中文会显示成 `b'...'`） |

过滤运算符实测支持：`eq`、`in`、`not_in`、`is_null`：

```text
--filter dept_level1:eq:测试部
   → WHERE SUBSTRING_INDEX(department_full_name, '/', 1) = '测试部'
--filter if_approve:in:0,1
   → WHERE if_approve IN ('0', '1')
--filter dept_level1:not_in:测试部,财务部
   → WHERE SUBSTRING_INDEX(department_full_name, '/', 1) NOT IN ('测试部', '财务部')
--filter dept_level1:is_null
   → WHERE SUBSTRING_INDEX(department_full_name, '/', 1) IS NULL
```

**注意：只能过滤维度，不能过滤指标**：

```text
--filter total_hours:gt:100
Error: Error during planning: Unknown filter dimension 'total_hours' in cube 'workhour_analysis'
```

需要在指标上做条件时，把条件做进视图（新增一个标记列）或写成新的指标。

### 2.7 写完怎么验证立方体

```powershell
& $wren cube list                                   # 看是否登记
& $wren cube describe workhour_analysis              # 看完整结构（JSON）
& $wren cube query --cube workhour_analysis --measures total_hours,user_count --dimensions dept_level1 --sql-only
& $wren cube query --cube workhour_analysis --measures total_hours --dimensions dept_level1 --limit 3 --output json
```

实测返回（注意 `SUM` 结果是字符串、`COUNT` 是数字）：

```text
{"work_date__month":1767225600000,"dept_level1":"\u65e0\u9521...","total_hours":"27136.70"}
```

时间维度返回 **epoch 毫秒**（`1767225600000` = 2026-01-01）。

---

## 3. 第三层：knowledge —— 把口径写给 AI

`knowledge/` 下的文件都是 **Markdown**（不是 YAML）。后端只读五个目录，且只认 `.md`：

| 目录 | 写什么 | 读者 | 生效时机 |
| --- | --- | --- | --- |
| `rules/` | 强制口径、默认过滤、单位 | `wren context instructions`、后端 | 下次提问 |
| `glossary/` | 术语、枚举取值含义 | MCP 知识工具 | 下次提问 |
| `metrics/` | 指标定义与公式 | MCP 知识工具 | 下次提问 |
| `caveats/` | 陷阱、反例、实测数字 | MCP 知识工具 | 下次提问 |
| `sql/` | 已验证的 NL→SQL 样例 | `wren memory recall`、后端记忆 | 建索引后 |

写成 `.yml`、或放到别的目录，等于没写。

### 3.1 `knowledge/knowledge.yml` 字段

| 字段 | 类型 | 含义 | 本库值 |
| --- | --- | --- | --- |
| `version` | 整数 | 知识库版本轴（与 `wren_project.yml` 的 `schema_version` 无关） | `1` |
| `description` | 字符串 | 知识库说明 | `"唯因特运营管理平台（witops）知识库 — 业务规则、查询示例、指标定义"` |

### 3.2 `rules/*.md`：强制口径

没有 front-matter，直接写 Markdown。结构建议：先给「正确写法」，再给「错误写法 + 实测数字」。

```markdown
## 员工在职口径（强制）

- 当前在职员工 = `do_department_user_detail WHERE deleted = 0`（实测 190 人，2026-09-19）。
- 不要用 `COUNT(DISTINCT user_id)`：含离职历史，实测 309。
- 不要用 `do_work_hour` 的去重人数：含离职者历史报工。
```

本库现有 3 个规则文件：`通用规则.md`、`业务域口径.md`、`报工与工时.md`。

### 3.3 `glossary/*.md`：术语与枚举

一条枚举写成「字段 → 编码 → 中文名 → 备注」，便于 AI 与人对齐。本库 `术语表.md` 约 28 KB，是枚举的唯一真源，结构是「一、术语对照 / 二、核心表字段说明 / 三、枚举字典」。缺陷状态这一节（节选）就是这样写的：

```markdown
### 3.2 缺陷状态 `do_bug.status` / `v_bug.status_name`
**平台新字典（2026-09-15 确认，只有 4 个值）**：
| 值 | 名称 | 说明 |
|---|---|---|
| 0 | 待开始 | |
| 1 | 进行中 | |
| 2 | **已结束** | 交付完成口径（旧口径曾叫"已上线"） |
| 3 | 待完善 | |

**旧流程码（新字典未定义，数据里仍有约 1.1 万行）**：
| 值 | 视图中的名称 | 旧口径含义 | 实测行数 |
|---|---|---|---|
| 4 | 已取消(历史码) | 已取消，统计时排除 | ~4560 |
| 99 | 关闭(历史码) | 归档 | ~6925 |

> 这些码**不要按新字典解读**；凡涉及状态口径，统一按「`status <> 2` 视为未结束」处理。
```

写法要点：**新码与历史码分开列**，历史码标注实测行数，并在结尾给一句可直接照做的口径结论。

### 3.4 `metrics/*.md`：指标定义

写清「名称 + 公式 + 分母 + 适用范围」，与本库立方体里的 `description` 保持一致。本库 `指标定义.md` 第 1.1 节：

```markdown
### 1.1 需求交付完成率

需求完成率 = 已完成需求数 / 有效需求总数 × 100%
已完成 = status = 14；有效 = deleted = 0 且 status <> 6（排除已取消）
```

对应立方体指标 `story_delivery.done_rate`，其 `description` 写的是「需求完成率（已完成 / 排除已取消后的需求数）」——**两处口径必须一致**，改一处就要改另一处。

### 3.5 `caveats/*.md`：陷阱

一条陷阱 = 现象 + 实测数字 + 正确做法。本库 `常见陷阱.md` 已积累 7 大类，是新人必读。

```markdown
### 5.4.1 立方体的时间维度是**左闭右开**，与 SQL 的 BETWEEN 不同

走立方体通道时，`name:granularity:start,end` 的语义是 `[start, end)`。
```

### 3.6 `sql/*.md`：已验证的 NL→SQL 样例

**带 front-matter**，字段如下：

| 字段 | 类型 | 必填 | 含义 | 样例 |
| --- | --- | --- | --- | --- |
| `nl` | 字符串 | 是 | 用户会怎么问（自然语言） | `月度工时统计，按月统计报工人数、总工时和人均工时` |
| `sql` | 多行字符串 | 是 | 已验证的 SQL | 见下 |
| `datasource` | 字符串 | 否 | 数据源方言 | `mysql` |
| `tags` | 列表 | 否 | 检索标签 | `[工时, 报工, 月度, 统计]` |
| `source` | 字符串 | 否 | 来源：`seed`（工具生成）或人工 | `seed` |

真实样例（`knowledge/sql/月度工时统计.md` 头部）：

```markdown
---
nl: 月度工时统计，按月统计报工人数、总工时和人均工时
sql: |
  SELECT DATE_FORMAT(date, '%Y-%m') AS ym,
    COUNT(DISTINCT user_id) AS users,
    ROUND(SUM(work_hour), 1) AS hours
  FROM do_work_hour_detail_extend
  WHERE deleted = 0
    AND date >= '2026-01-01' AND date <= '2026-08-31'
  GROUP BY ym
  ORDER BY ym
datasource: mysql
tags:
  - 工时
  - 报工
  - 月度
  - 统计
source: seed
---

# 月度工时统计

## 查询说明
按自然月统计报工人数与总工时。**必须使用精确的日期区间**……
```

用命令生成（自动写文件 + 建索引），实测：

```powershell
& $wren memory store --nl "按一级部门统计在职员工人数" --sql "SELECT SUBSTRING_INDEX(department_full_name, '/', 1) AS dept, COUNT(*) AS c FROM v_workhour GROUP BY dept ORDER BY c DESC"
```

```text
Stored: <项目目录>\knowledge\sql\query.md
```

生成的文件内容：

```markdown
---
nl: 按一级部门统计在职员工人数
sql: SELECT SUBSTRING_INDEX(department_full_name, '/', 1) AS dept, COUNT(*) AS c FROM
  v_workhour GROUP BY dept ORDER BY c DESC
source: user
---
```

注意两点：

- `source` 为 `user`（人工存）或 `seed`（工具预置），本库 `月度工时统计.md` 等文件为 `seed`。
- 工具给的文件名可能是通用名（实测 `query.md`）。**入库前请改成中文主题名**（如 `人员产出与工时.md`），并在正文补「查询说明 / 变体 / 注意事项」三节。

配套命令：

```powershell
& $wren memory status     # 后端与索引状态（本机：Backend: lancedb）
& $wren memory check      # knowledge/sql/*.md 与索引是否有漂移
& $wren memory index      # 知识库变更后重建索引
```

---

## 4. 附：models 与 relationships（不用你写，但要看得懂）

### 4.1 `models/<表名>/metadata.yml` 顶层字段

| 字段 | 类型 | 含义 | 本库样例 |
| --- | --- | --- | --- |
| `name` | 字符串 | 模型名（= 物理表名，小写） | `do_bug` |
| `table_reference.table` | 字符串 | 真实物理表名 | `do_bug`（也可写 `catalog` / `schema`） |
| `columns` | 列表 | 列定义 | 见 4.2 |
| `primary_key` | 字符串 | 主键列名 | `id` |
| `cached` | 布尔 | 是否缓存（本库全部 `false`） | `false` |
| `properties.description` | 字符串 | 表的中文含义 | `"bug缺陷"` |

### 4.2 `columns[]` 字段

| 字段 | 类型 | 含义 | 取值 |
| --- | --- | --- | --- |
| `name` | 字符串 | 列名 | `status` |
| `type` | 字符串 | 列类型 | `INTEGER` / `VARCHAR` / `TIMESTAMP` / `DOUBLE` / `DATE` |
| `is_calculated` | 布尔 | 是否计算列（视图派生列用） | 本库全为 `false` |
| `not_null` | 布尔 | 是否非空 | `true` / `false` |
| `is_primary_key` | 布尔 | 是否主键 | `true` / `false` |
| `hidden` | 布尔 | **是否对查询隐藏**；隐藏后不可引用 | `true`（本库 14 个列被隐藏） |
| `properties.description` | 字符串 | 列的中文含义与取值映射 | `"状态,映射关系为 0:待开始,1:进行中,2:已结束,3:待完善"` |

`hidden` 的真实用途：字段作废但物理列还在，就标 `hidden: true`，避免 AI 与查询引用它。本库两个真实例子（`models/do_department_user_detail/metadata.yml` 与 `models/do_bug/metadata.yml`）：

```yaml
# 例 1：字段停用（do_department_user_detail.position）
- name: position
  type: VARCHAR
  is_calculated: false
  not_null: false
  is_primary_key: false
  hidden: true
  properties:
    description: 目前不使用该字段

# 例 2：业务已迁移（do_bug.product_line，本库共 4 个字段作废）
- name: product_line
  type: INTEGER
  is_calculated: false
  not_null: false
  is_primary_key: false
  hidden: true
  properties:
    description: 产品线
```

`do_bug` 中被标隐藏的 4 个字段是 `project_collect`、`product_line`、`industry_dic_config_code`、`notify_user_ids`；`knowledge/glossary/术语表.md` 里对应写成「**【作废】**模型里已标 `hidden: true`」，两处必须同步。当前全库共 14 个列被隐藏。

类型对照（生成脚本 `gen_models_mysql.py` 的映射表）：

| MySQL 原始类型 | 语义库类型 |
| --- | --- |
| `INT` / `BIGINT` / `TINYINT` / `SMALLINT` / `MEDIUMINT` | `INTEGER` |
| `VARCHAR` / `CHAR` / `TEXT` / `LONGTEXT` / `MEDIUMTEXT` | `VARCHAR` |
| `DECIMAL` / `NUMERIC` / `FLOAT` / `DOUBLE` | `DOUBLE` |
| `DATETIME` / `TIMESTAMP` | `TIMESTAMP` |
| `DATE` | `DATE` |
| `BOOLEAN` / `BOOL` | `BOOLEAN` |

### 4.3 `relationships.yml` 字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `relationships` | 列表 | 关系定义数组 |
| `relationships[].name` | 字符串 | 关系名 |
| `relationships[].models` | 字符串列表 | 参与关系的两个模型名 |
| `relationships[].join_type` | 字符串 | `MANY_TO_ONE` / `ONE_TO_ONE` |
| `relationships[].condition` | 字符串 | JOIN 条件，如 `orders.customer_id = customers.customer_id` |

本库为空数组，因为业务表之间没有数据库外键：

```yaml
# Auto-generated from database foreign keys
relationships: []
```

所以**所有关联都必须写在视图的 JOIN 里**，立方体拿不到任何隐式 JOIN。

---

## 5. 字段速查总表

| 文件 | 字段 | 类型 | 必填 | 一句话含义 |
| --- | --- | --- | --- | --- |
| `wren_project.yml` | `schema_version` | 整数 | 是 | 项目布局版本（本库 5） |
| | `name` / `version` | 字符串 | 是 | 项目名 / 版本 |
| | `catalog` / `schema` | 字符串 | 是 | 引擎命名空间（非数据库名） |
| | `data_source` | 字符串 | 是 | 数据源类型（`mysql`） |
| | `profile` | 字符串 | 否 | 绑定的连接 profile（本地绑定，勿提交） |
| | `properties.description` | 字符串 | 否 | 项目说明 |
| `views/*/metadata.yml` | `name` | 字符串 | 是 | 视图名，全局唯一 |
| | `dialect` | 字符串 | 否 | SQL 方言 |
| | `properties.description` | 字符串 | 建议 | 粒度 + 过滤说明 |
| `views/*/sql.yml` | `statement` | 多行字符串 | 是 | 视图 SQL |
| `cubes/*/metadata.yml` | `name` | 字符串 | 是 | 立方体名，全局唯一 |
| | `description` | 字符串 | 建议 | 能回答什么问题 |
| | `base_object` | 字符串 | 是 | 绑定模型或视图 |
| | `measures[].name` | 字符串 | 是 | 指标名 |
| | `measures[].expression` | 字符串 | 是 | 聚合表达式 |
| | `measures[].type` | 字符串 | 是 | `BIGINT` / `DOUBLE` |
| | `measures[].description` | 字符串 | 建议 | 口径（含分母） |
| | `dimensions[].name` | 字符串 | 是 | 维度名 |
| | `dimensions[].expression` | 字符串 | 是 | 取值表达式 |
| | `dimensions[].type` | 字符串 | 是 | `string` / `int` |
| | `dimensions[].description` | 字符串 | 建议 | 含义与编码 |
| | `time_dimensions[].name` | 字符串 | 是 | 时间维度名 |
| | `time_dimensions[].expression` | 字符串 | 是 | 时间列 |
| | `time_dimensions[].type` | 字符串 | 是 | `date` |
| | `time_dimensions[].levels` | 列表 | 否 | `[year, quarter, month, week, day]` |
| | `hierarchies` | 映射 | 否 | `层级名: [维度…]` |
| `knowledge/knowledge.yml` | `version` / `description` | 整数 / 字符串 | 否 | 知识库版本与说明 |
| `knowledge/sql/*.md` | `nl` / `sql` | 字符串 | 是 | 问题与已验证 SQL |
| | `datasource` / `tags` / `source` | 字符串 / 列表 | 否 | 方言 / 标签 / 来源 |
| `models/*/metadata.yml` | `name` / `table_reference.table` | 字符串 | 是 | 模型名与物理表名 |
| | `columns[]` | 列表 | 是 | 列定义（见 4.2） |
| | `primary_key` / `cached` / `properties` | 字符串 / 布尔 / 映射 | 否 | 主键 / 缓存 / 说明 |
| `relationships.yml` | `relationships[].name/models/join_type/condition` | 字符串 / 列表 | 否 | 关系定义 |

---

## 6. 写完必跑，以及会遇到的报错

```powershell
$env:PYTHONUTF8 = '1'                       # Windows 必做，否则 UnicodeDecodeError: 'gbk'
cd D:\code_work_space\llm\witops-wrenai     # cube 系命令只认当前目录或 WREN_PROJECT_HOME
& $wren context validate                    # 1. 结构
& $wren context build                       # 2. 编译
& $wren cube list                           # 3. 登记
& $wren cube query --cube <c> --measures <m> --sql-only   # 4. SQL 是否符合预期
& $wren cube query --cube <c> --measures <m> --limit 5 --output json   # 5. 真跑
& $wren --sql "SELECT ... FROM <view> LIMIT 5" --output json           # 6. 视图层复核
```

| 报错 | 原因 | 修法 |
| --- | --- | --- |
| `UnicodeDecodeError: 'gbk' codec ...` | Windows 默认编码 | 设 `PYTHONUTF8=1` |
| `view missing 'name'` / `duplicate name 'x'` | 视图名缺失或重名 | 补名 / 改名 |
| `view missing 'statement' ...` | 没写 SQL | 在 `sql.yml` 写 `statement` |
| `cube missing 'base_object'` | 立方体未绑定 | 补 `base_object` |
| `base_object 'x' is not a defined model or view` | 名字错或未 build | 核对名字并重新 build |
| `cube has no measures` | 没有指标 | 至少加一个 measure |
| `hierarchies.x references unknown dimension 'y'` | 层级引用了未声明维度 | 先声明该维度 |
| `table 'wren.public.v_x' not found` | 改完没 build，或视图引用了视图 | 重新 build；视图只引用物理表 |
| `Invalid function 'date'` / `'group_concat'` | 用了不支持的内置函数 | 见 1.3 |
| `Column in SELECT must be in GROUP BY ...` | 非聚合列未分组 | `MAX()` 包裹 + `GROUP BY` 主键 |
| `Unknown filter dimension 'total_hours'` | 过滤了指标 | 只能过滤维度，条件做进视图 |
| 查询连到了别的库 | 项目未绑定 profile，回退全局 active | `wren context set-profile "wren_mcp_wrenai_WIT运营管理平台数据库"` |
| 立方体时间区间少一天 | 区间是左闭右开 | 结束日期写下一天 |
| 新视图/立方体没生效 | 忘了 `context build`，或后端需要重启 | 语义库**内容**更新无需重启；新增/删除语义库项目需重启后端 |
