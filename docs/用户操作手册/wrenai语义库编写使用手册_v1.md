---
title: Wrenai 语义库编写使用手册（witops 运营管理平台）
type: tutorial
audience: [语义库编写人员, 数据建模工程师, 新入职工程师]
runs: yes
verified_on: 2026-09-19
sources:
  - D:\code_work_space\llm\witops-wrenai\wren_project.yml
  - D:\code_work_space\llm\witops-wrenai\views\v_story\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\views\v_story\sql.yml
  - D:\code_work_space\llm\witops-wrenai\views\v_workhour\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\views\v_workhour\sql.yml
  - D:\code_work_space\llm\witops-wrenai\cubes\workhour_analysis\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\cubes\story_delivery\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\models\do_bug\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\models\do_department_user_detail\metadata.yml
  - D:\code_work_space\llm\witops-wrenai\knowledge\caveats\常见陷阱.md
  - D:\code_work_space\llm\witops-wrenai\knowledge\sql\月度工时统计.md
  - D:\code_work_space\llm\nl2sql\src\agent\tools\mcp_tool.py
  - D:\code_work_space\llm\nl2sql\src\api\wren_semantic.py
---

# Wrenai 语义库编写使用手册（witops 运营管理平台）

> 用途：员工培训与日常作业手册 —— 教你从零写出一个能被 AI 正确使用的语义库视图与立方体。
> 适用对象：语义库编写人员、数据建模工程师、需要维护业务口径的同学。
> 前置条件：能访问 witops 建模库、能在本机跑命令行。
> 维护说明：语义库结构或 wren 版本变化时，请同步更新本文并修改 `verified_on`。

**范围说明**

- 本文覆盖：`views/`、`cubes/`、`knowledge/` 三层的编写与验证。
- 本文不覆盖：`models/`（由代码从数据库表结构自动生成，**不要手改**）、平台前端「语义库管理」界面操作（见 `系统功能清单_客户培训.md` 第 3.7 节）。
- 相关文档：`SQL通道-SQL生成实现思路方案.md`（一条问题如何变成物理 SQL）、`nl2sql后端与Agent设计实现方案.md`（后端与 Agent 全貌）。

---

## 第一部分 认识语义库

### 1.1 语义库解决什么问题

数据库里的表名叫 `do_story`，字段叫 `status`，值是 `14`。AI 不知道 `14` 是「已完成」，也不知道 `deleted=1` 的行必须剔除。
语义库就是把这层业务含义**预先写死**成结构化定义，让 AI 每次查询都按同一套口径理解数据。

一句话：**语义库是给 AI 看的业务字典 + 查询模板。**

### 1.2 一条问题是怎么走通的

```text
用户提问
  → 子智能体选择「语义层通道」
  → MCP 工具 query_cube（按立方体查）或 run_sql（按视图查）
  → wren-core 把立方体查询翻译成视图级 SQL
  → wren 引擎把视图展开成物理 SQL（CTE + 真实表名）
  → MySQL 执行，返回结果
```

你写的每一层都有明确位置：

| 层 | 你写什么 | 引擎拿它做什么 |
| --- | --- | --- |
| `models/` | 不写（代码生成） | 告诉引擎「有哪些物理表、字段、类型、描述」 |
| `views/` | 原生 SQL（视图） | 把软删过滤、枚举中文名、常用 JOIN 固化下来 |
| `cubes/` | 指标（measures）、维度（dimensions） | 让 AI 用「指标 + 维度」提问，而不是拼 SQL |
| `knowledge/` | Markdown 业务规则与样例 | 让 AI 理解口径、少犯错、复用已验证写法 |

### 1.3 目录结构

```text
witops-wrenai/
├─ wren_project.yml                 # 项目元信息：名称、数据源、绑定的连接 profile
├─ relationships.yml                # 表间关系（本库为空：业务表之间没有数据库外键）
├─ models/<表名>/metadata.yml        # 48 张表，代码自动生成，勿手改
├─ views/<视图名>/metadata.yml       # 视图声明（名称、方言、说明）
├─ views/<视图名>/sql.yml            # 视图 SQL（statement）
├─ cubes/<立方体名>/metadata.yml     # 指标 / 维度 / 时间维度定义
├─ knowledge/
│   ├─ knowledge.yml                # 知识库版本声明
│   ├─ rules/*.md                   # 业务规则（强制口径）
│   ├─ glossary/*.md                # 术语表（枚举值含义）
│   ├─ metrics/*.md                 # 指标定义
│   ├─ caveats/*.md                 # 常见陷阱
│   └─ sql/*.md                     # 已验证的 NL→SQL 样例
└─ target/mdl.json                  # wren context build 产物，引擎真正读取的文件
```

当前规模：48 个模型、5 个视图（`v_story` / `v_bug` / `v_iteration_burndown` / `v_project` / `v_workhour`）、6 个立方体。

### 1.4 两种工作方式

| 方式 | 适合场景 | 入口 |
| --- | --- | --- |
| 平台界面 | 改少量知识条目、看构建状态、快速校验 | 平台「数据库设置 → 语义库」选项卡（详见 `系统功能清单_客户培训.md` 3.7） |
| 本地 Git + wren CLI | 新增/重构视图与立方体、批量修改、需要反复试跑 | 本文第二部分起的命令行流程 |

界面保存知识条目后即时生效；用命令行改动后必须执行 `wren context build`，引擎才看得到。

---

## 第二部分 环境准备

### 2.1 wren CLI

本机 CLI 位置（随 nl2sql 项目虚拟环境安装）：

```text
D:\code_work_space\llm\nl2sql\.venv\Scripts\wren.exe
```

版本：`wrenai 0.13.0`，内核 `wren_core 0.7.1`。

为方便输入，先在当前 PowerShell 会话里定义别名：

```powershell
$wren = "D:\code_work_space\llm\nl2sql\.venv\Scripts\wren.exe"
```

### 2.2 Windows 必做：开启 UTF-8 模式

语义库文件全是 UTF-8 中文。Windows 版 Python 默认用 GBK 读取，会直接崩溃：

```text
UnicodeDecodeError: 'gbk' codec can't decode byte 0x89 in position 108: illegal multibyte sequence
```

每次开新终端，先执行：

```powershell
$env:PYTHONUTF8 = '1'
```

把这条写进团队启动脚本或 VS Code 任务，可以避免忘记。

### 2.3 绑定数据库连接 profile

`wren` 需要一个连接配置才能真的查库。本机 `~/.wren/profiles.yml` 里已有本库的 profile：

```text
wren_mcp_wrenai_WIT运营管理平台数据库   →   mysql 192.168.19.100:3306/witops（只读账号）
```

把它绑定到项目（**每个克隆下来的仓库都要绑一次**）：

```powershell
cd D:\code_work_space\llm\witops-wrenai
& $wren context set-profile "wren_mcp_wrenai_WIT运营管理平台数据库"
```

执行后 `wren_project.yml` 会多出一行：

```yaml
profile: wren_mcp_wrenai_WIT运营管理平台数据库
```

注意事项：

- 不绑定也不会报错，但 CLI 会**回退到全局 active profile**（本机当前是 `chinook`，指向 ClickHouse），查询会连到错误的库。生产后端不受影响，它启动 MCP 时用 `--profile` 显式指定。
- `profile:` 是本机凭据绑定，**建议留在本地、不要提交**；确需统一时由团队约定统一的 profile 名。
- 检查连接是否解析正确（敏感字段自动打码）：

```powershell
& $wren profile debug
& $wren profile list
```

### 2.4 命令在哪个目录执行

规则不一致，务必记住：

| 命令族 | 项目定位方式 |
| --- | --- |
| `wren context validate` / `build` / `show` | 支持 `--path <项目目录>`；也可由 `WREN_PROJECT_HOME` 或当前目录推断 |
| `wren cube list` / `describe` / `query` | **不认 `--path`**，只认当前目录或 `WREN_PROJECT_HOME` |

所以要么先 `cd` 进仓库，要么设置环境变量：

```powershell
$env:WREN_PROJECT_HOME = "D:\code_work_space\llm\witops-wrenai"
```

### 2.5 五分钟自检

```powershell
cd D:\code_work_space\llm\witops-wrenai
$env:PYTHONUTF8 = '1'
& $wren context validate
& $wren context build
& $wren cube list
```

期望输出（实测，路径随本机项目位置变化）：

```text
Valid — 48 models, 5 views, 0 relationships.
Built: 48 models, 5 views → D:\code_work_space\llm\witops-wrenai\target\mdl.json
bug_quality (base: v_bug)
  measures: bug_count, finished_count, ...
```

---

## 第三部分 手把手：写出「员工在职」视图与立方体

这一部分是从零到跑通的完整练习。请逐条执行，不要跳步。

### 3.1 目标

交付两个文件，实现「按部门统计员工规模与在职情况」：

```text
views/v_staff/metadata.yml      视图声明
views/v_staff/sql.yml           视图 SQL
cubes/staff_overview/metadata.yml   立方体定义
```

验收标准：

1. `wren context validate` 通过，模型/视图计数 +1。
2. `wren cube query --sql-only` 能打出视图级 SQL。
3. 真跑能返回部门维度结果。
4. 员工总数与业务口径一致（本节 3.8 会说明为什么这一步最容易出错）。

### 3.2 第 1 步：确认物理表与可用字段

先看模型里有什么。以本次示例用的员工表为例：

```powershell
Get-Content .\models\do_department_user_detail\metadata.yml
```

关键规则：

- **只能引用已建模的表**（48 张）。库里有约 151 张表，未建模的表查询不会被规划。
- **带 `hidden: true` 的字段不可引用**。例如 `do_department_user_detail.position` 标注了 `hidden: true`，用它做维度会失败；请改用 `position_id` 关联或其他已开放字段。
- 字段的业务含义看 `properties.description`。描述质量直接决定 AI 选字段的准确率。

本示例要用到的三张表：

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `do_department_user_detail` | 员工主数据 | `id`、`user_id`、`name`、`job_number`、`gender`、`entry_date`、`leave_date`、`deleted` |
| `do_department_user` | 员工-部门关联 | `user_id`、`department_id`、`deleted` |
| `do_department` | 部门 | `id`、`name`、`deleted` |

### 3.3 第 2 步：写视图声明 `views/v_staff/metadata.yml`

```yaml
# 员工在职视图（培训示例）
# 为什么要建视图：do_department_user_detail 含历史快照（实测 1141 行），
# 其中 deleted=0 仅 190 行，直接统计会虚高 6 倍。
name: v_staff
dialect: mysql
properties:
  description: "员工在职视图（deleted=0，1 行/员工）— 含部门名称与在职标记"
```

要点：

- `name` 必须全局唯一（不能和任何模型、视图重名）。
- `name` 建议用 `v_` 前缀，与物理表区分。
- `dialect: mysql` 声明视图 SQL 的方言。
- `properties.description` 是给 AI 看的说明，写清「粒度 + 过滤条件」。

### 3.4 第 3 步：写视图 SQL `views/v_staff/sql.yml`

```yaml
statement: |
  SELECT
    u.id                        AS staff_id,
    MAX(u.user_id)              AS user_id,
    MAX(u.name)                 AS user_name,
    MAX(u.job_number)           AS job_number,
    MAX(u.gender)               AS gender,
    MAX(u.entry_date)           AS entry_date,
    MAX(u.leave_date)           AS leave_date,
    MAX(CASE WHEN u.leave_date IS NULL THEN 1 ELSE 0 END) AS is_active,
    MAX(d.name)                 AS department_name,
    COUNT(DISTINCT du.department_id) AS department_count
  FROM do_department_user_detail u
  LEFT JOIN do_department_user du
    ON du.user_id = u.user_id AND du.deleted = 0
  LEFT JOIN do_department d
    ON d.id = du.department_id AND d.deleted = 0
  WHERE u.deleted = 0
  GROUP BY u.id
```

逐段解释：

1. **`WHERE u.deleted = 0`**：软删过滤。这是本库最高频的错误来源，任何视图都要先想清楚过滤条件。
2. **`GROUP BY u.id` + `MAX(...)`**：一个员工可能挂多个部门（`do_department_user` 一对多）。`GROUP BY u.id` 保证**一行一个员工**、不放大行数；所有非聚合列用 `MAX()` 包裹，满足规划器「非聚合列必须出现在 GROUP BY 中」的要求。
3. **`COUNT(DISTINCT ...)`**：关联表的规模用去重计数带回视图，后续立方体的计数与求和才是精确的。
4. **每张 JOIN 表都带 `deleted = 0`**，且条件写在 `ON` 里而不是 `WHERE`，避免把 `LEFT JOIN` 退化成内连接。
5. **枚举中文名就地映射**：本例的 `is_active` 是布尔标记；若字段是编码枚举，用 `CASE` 映射（见附录 B.2）。

### 3.5 第 4 步：编译并校验

```powershell
& $wren context validate
& $wren context build
```

实测输出：

```text
Valid — 48 models, 6 views, 0 relationships.
Built: 48 models, 6 views → D:\code_work_space\llm\witops-wrenai\target\mdl.json
```

`validate` 只做结构与描述级检查。**视图 SQL 是否真能规划**，要继续做第 6 步。

### 3.6 第 5 步：写立方体 `cubes/staff_overview/metadata.yml`

```yaml
name: staff_overview
description: "员工规模与在职情况立方体 — 按部门/在职状态/入职时间下钻"
base_object: v_staff
measures:
  - name: staff_count
    expression: "COUNT(*)"
    type: BIGINT
    description: "员工数（deleted=0）"
  - name: active_count
    expression: "SUM(is_active)"
    type: BIGINT
    description: "在职人数（leave_date 为空）"
  - name: left_count
    expression: "SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END)"
    type: BIGINT
    description: "已离职人数"
  - name: multi_dept_count
    expression: "SUM(CASE WHEN department_count > 1 THEN 1 ELSE 0 END)"
    type: BIGINT
    description: "挂在 2 个及以上部门的员工数"
dimensions:
  - name: user_name
    expression: "user_name"
    type: string
    description: "姓名"
  - name: job_number
    expression: "job_number"
    type: string
    description: "工号"
  - name: gender
    expression: "gender"
    type: string
    description: "性别"
  - name: is_active
    expression: "is_active"
    type: int
    description: "是否在职（1=在职，0=已离职）"
  - name: department_name
    expression: "department_name"
    type: string
    description: "所属部门名称"
time_dimensions:
  - name: entry_date
    expression: "entry_date"
    type: date
    description: "入职日期"
    levels: [year, quarter, month, day]
```

要点：

- `base_object` 必须指向**已存在的模型或视图**；写错会报 `base_object 'x' is not a defined model or view`。
- 立方体只能绑定一个 `base_object`，**不支持在立方体里再写 JOIN**。所有关联都要在视图里做完。
- 每个指标/维度都要写 `description`，这是 AI 选对指标的凭据。
- 命名不要复用：指标名不要与它聚合的列同名（历史上出现过 `circular dependency detected in measure expressions`，改名为 `work_hour_total` 即可解决）。

### 3.7 第 6 步：三步验证

**第一步，看它有没有被登记：**

```powershell
& $wren cube list
& $wren cube describe staff_overview
```

**第二步，只看它翻译出的视图级 SQL（不连库，最快）：**

```powershell
& $wren cube query --cube staff_overview --measures staff_count,active_count --dimensions department_name --sql-only
```

实测输出：

```sql
SELECT department_name AS department_name, COUNT(*) AS staff_count, SUM(is_active) AS active_count FROM v_staff GROUP BY 1
```

**第三步，真跑一次：**

```powershell
& $wren cube query --cube staff_overview --measures staff_count,active_count --dimensions department_name --limit 5 --output json
```

实测输出（中文以 `\uXXXX` 转义，属正常）：

```text
{"department_name":"\u7814\u53d1","staff_count":9,"active_count":"9"}
{"department_name":"\u8d22\u52a1\u90e8","staff_count":2,"active_count":"2"}
{"department_name":"\u8d28\u91cf\u8fd0\u8425\u90e8","staff_count":3,"active_count":"3"}
{"department_name":"\u89e3\u51b3\u65b9\u6848\u90e8","staff_count":9,"active_count":"9"}
```

注意 `--output table` 与 `--output csv` 会把中文显示成 `b'\xe6\x97\xa0...'` 的字节串，**核对数据请统一用 `--output json`**。

### 3.8 第 7 步：和业务对数（最容易翻车的一步）

同一个问题，写法不同结果差 6 倍。用视图与源表各查一次：

```powershell
& $wren --sql "SELECT COUNT(*) AS staff_count, SUM(is_active) AS active_count, SUM(CASE WHEN is_active=0 THEN 1 ELSE 0 END) AS left_count FROM v_staff" --output json --quiet
& $wren --sql "SELECT COUNT(*) AS raw_rows, COUNT(DISTINCT user_id) AS raw_users FROM do_department_user_detail" --output json --quiet
```

实测输出：

```text
{"staff_count":190,"active_count":"190","left_count":"0"}
{"raw_rows":1141,"raw_users":309}
```

结论：**190 才是「当前在职」的口径**，1141 是含历史快照的虚高值。这条口径已写入 `knowledge/caveats/常见陷阱.md` 第 4.1 节。

对数三问，每写一个指标都要问：

1. 分母是什么？排除哪些状态？（例如需求完成率要排除「已取消」）
2. 时间窗口是闭区间还是半开区间？两端时间是否齐全？
3. 行数会不会被 JOIN 放大？（用 `COUNT(DISTINCT)` 或预聚合，并在视图里就固定粒度）

数据是活的：本次实测 190 人，而 `knowledge/caveats` 记录为 191 人。**写进文档的数字要带日期**。

### 3.9 第 8 步：沉淀知识（推荐）

新写的视图/立方体，同步补 2~3 条知识条目：

| 写到哪 | 写什么 | 被谁读取 |
| --- | --- | --- |
| `knowledge/glossary/*.md` | 新出现的枚举值含义（如 `is_active`） | MCP 知识工具 `get_all_knowledge` |
| `knowledge/rules/*.md` | 强制口径（如「在职只认 `deleted=0`」） | `wren context instructions` 与后端 |
| `knowledge/metrics/*.md` | 指标定义与公式 | MCP 知识工具 |
| `knowledge/sql/*.md` | 已验证的 NL→SQL 样例 | `wren memory recall` 与后端记忆 |
| `knowledge/caveats/*.md` | 踩过的坑 | MCP 知识工具 |

`knowledge/` 下**只认 `.md` 文件**，且后端只读 `glossary` / `metrics` / `rules` / `sql` / `caveats` 五个目录。写成 `.yml` 或在别处新建目录，等于没写。

沉淀一条 NL→SQL 样例：

```powershell
& $wren memory store --nl "按部门统计在职员工人数" --sql "SELECT department_name, COUNT(*) AS c FROM v_staff GROUP BY department_name"
```

它会写入 `knowledge/sql/<slug>.md` 并尝试建索引。相关命令：

```powershell
& $wren memory status     # 看后端与索引状态
& $wren memory check      # 看 knowledge/sql/*.md 与索引是否有漂移
& $wren memory index      # 重建索引（改了知识库之后）
```

### 3.10 第 9 步：提交与发布

```powershell
git checkout -b feat/semantic-staff-overview
git add views/v_staff cubes/staff_overview knowledge/
git commit -m "feat(semantic): 新增员工在职视图与员工规模立方体"
git push origin feat/semantic-staff-overview
```

生效条件（后端行为，来自 `src/agent/tools/mcp_tool.py`）：

- 语义库**内容更新**（`git pull` + 重新 `build`）：**无需重启后端**。每次工具调用都会新起 MCP 子进程读取 `target/mdl.json`。
- **新增/删除语义库项目**、或改某个库的 `wren_project` 关联：**必须重启后端**才生效。
- `target/mdl.json` 是构建产物但需要跟随提交（后端直接读它）；改完不 build 就等于没改。

---

## 第四部分 常见任务怎么做

### 4.1 新增一个视图

1. 建目录 `views/<视图名>/`。
2. 按 3.3、3.4 写 `metadata.yml` 与 `sql.yml`。
3. `wren context validate` → `wren context build` → `wren --sql "SELECT * FROM <视图名> LIMIT 5"`。

### 4.2 给已有视图加字段

1. 在 `sql.yml` 的 `SELECT` 里加列（非聚合列记得包 `MAX()`，或加进 `GROUP BY`）。
2. `wren context build`。
3. 在要用它的立方体里加同名维度（否则 AI 看不到这个字段）。

### 4.3 新增指标（measure）

在立方体 `measures` 下追加，命名用「业务含义 + 单位」，例如 `avg_resolve_days`：

```yaml
  - name: avg_resolve_days
    expression: "AVG(DATEDIFF(complete_time, mention_time))"
    type: DOUBLE
    description: "平均解决周期（天，仅统计两端时间齐全的缺陷）"
```

除法一律防除零：`SUM(...) * 1.0 / NULLIF(COUNT(*), 0)`。

### 4.4 新增时间维度并写对区间

```yaml
  - name: complete_time
    expression: "complete_time"
    type: date
    description: "实际完成时间"
    levels: [year, quarter, month, day]
```

查询时的时间区间是 **左闭右开**：

```powershell
& $wren cube query --cube workhour_analysis --measures total_hours --time-dimension work_date:month:2026-01-01,2026-04-01
```

上面表示 1 月 1 日（含）到 4 月 1 日（不含），即一整个第一季度。
如果按 SQL 直觉写成 `2026-03-31`，会**静默漏掉 3 月 31 日**的数据（实测工时少算 1400 小时）。这条已写入 `knowledge/caveats/常见陷阱.md` 第 5.4.1 节。

### 4.5 新增枚举中文名映射

在视图 SQL 里用 `CASE` 就地映射，同时保留原始码：

```sql
    MAX(s.status)     AS status_code,
    MAX(COALESCE(CASE s.status
      WHEN 14 THEN '已完成' WHEN 6 THEN '已取消' WHEN 99 THEN '关闭'
      ELSE CONCAT('其他(', s.status, ')')
    END, '未设置'))   AS status_name,
```

- 字典来自系统管理库、语义库无法直连时，用「暂不映射」策略：只带原始码，并在 `knowledge/glossary` 注明「字典暂缺，勿猜中文名」。
- 不要凭字段名猜中文名。顺序错一位就是错误结论。

### 4.6 新增知识条目

直接写 Markdown，中文标题 + 表格即可。示例（`knowledge/rules/`）：

```markdown
## 员工在职口径（强制）

- 当前在职员工 = `do_department_user_detail WHERE deleted = 0`。
- 不要用 `COUNT(DISTINCT user_id)`（含离职历史，实测 309）。
- 不要用 `do_work_hour` 的去重人数（含离职者历史报工）。
```

### 4.7 改名或删除视图/立方体

- 改名 = 破坏性变更。调用方（AI 提示词、`knowledge/sql` 样例、其他立方体）可能仍引用旧名。
- 删除前先全局搜索：`git grep "<名称>"`。
- 已被废弃的宽表（如 `do_story_wide`、`do_bug_wide`）只从语义库移除文件，**物理表不动**。
- 删除后必须 `wren context build`，并确认 `wren cube list` 里不再出现。

### 4.8 改口径时要同步的地方

口径改动往往牵一发而动全身，按此清单同步：

| 位置 | 同步内容 |
| --- | --- |
| `views/*/sql.yml` | 过滤条件、映射、粒度 |
| `cubes/*/metadata.yml` | 指标表达式与描述（分母变了必须改描述） |
| `knowledge/glossary/*.md` | 枚举含义 |
| `knowledge/rules/*.md` | 强制口径 |
| `knowledge/metrics/*.md` | 指标公式 |
| `knowledge/sql/*.md` | 受影响的历史样例 |
| `knowledge/caveats/*.md` | 新增坑或修订旧结论 |

---

## 第五部分 规范与限制（参考）

### 5.1 文件字段参考

**视图 `views/<名>/metadata.yml`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 视图名，全局唯一（不可与模型、视图重名） |
| `dialect` | 否 | 方言，本库固定 `mysql` |
| `properties.description` | 强烈建议 | 粒度 + 过滤条件说明 |

**视图 `views/<名>/sql.yml`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `statement` | 是 | 原生 SQL（`statement: \|` 块） |

**立方体 `cubes/<名>/metadata.yml`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 立方体名，全局唯一 |
| `description` | 强烈建议 | 一句话说明能回答什么问题 |
| `base_object` | 是 | 绑定的模型或视图名 |
| `measures[]` | 是（空会告警） | `name` / `expression` / `type` / `description` |
| `dimensions[]` | 否 | 同上，`type` 常用 `string` / `int` |
| `time_dimensions[]` | 否 | 额外支持 `levels`（如 `[year, month, day]`） |
| `hierarchies` | 否 | 层级钻取，层级名必须是已声明的维度 |

**知识库 `knowledge/**/*.md`**

| 目录 | 内容 | 读取方 |
| --- | --- | --- |
| `rules/` | 强制业务规则 | `wren context instructions`、后端 |
| `glossary/` | 术语与枚举含义 | MCP 知识工具 |
| `metrics/` | 指标定义 | MCP 知识工具 |
| `caveats/` | 陷阱与反例 | MCP 知识工具 |
| `sql/` | NL→SQL 样例（带 front-matter） | `wren memory recall`、后端记忆 |

`knowledge/sql/*.md` 的 front-matter 约定：

```yaml
---
nl: 月度工时统计，按月统计报工人数、总工时和人均工时
sql: |
  SELECT ...
datasource: mysql
tags: [工时, 报工, 月度]
source: seed
---
```

### 5.2 视图 SQL 硬限制（均为实测）

| 限制 | 现象 / 报错原文 | 正确做法 |
| --- | --- | --- |
| 非聚合列必须出现在 GROUP BY | `Column in SELECT must be in GROUP BY or an aggregate function: ... column "x" must appear in the GROUP BY clause` | 用 `MAX(x)` 包裹，并 `GROUP BY` 主键 |
| 不支持 `DATE()` | `Invalid function 'date'. Did you mean 'atan'?` | 用 `CAST(x AS DATE)` |
| 不支持 `GROUP_CONCAT` | `Invalid function 'group_concat'. Did you mean 'array_concat'?` | 名单类字段不放视图，需要时直接查关联表 |
| 不能引用其他视图 | `table 'wren.public.v_workhour' not found` | 视图只能引用 `models/` 里的物理表；公共逻辑各自重写 |
| 派生表（`FROM (SELECT ...)`）风险高 | 规划期可能报 `No field named ...`（列解析失败） | 统一用扁平 `JOIN` 写法，本库 5 个视图均如此 |
| 指标名与列名冲突 | `circular dependency detected in measure expressions`（历史报错） | 指标名不要与它聚合的列同名 |
| 视图列数 / 行数 | 视图**不放大行数**是设计底线 | 多对多关联用 `COUNT(DISTINCT)` 预聚合 |

`wren context validate` 通过 ≠ 视图可规划：以上限制多数在 `build` 之后的**规划期**才暴露。所以每次改完视图，必须跑一次第 3.7 节的验证。

### 5.3 命名规范

| 对象 | 规范 | 示例 |
| --- | --- | --- |
| 视图目录/名称 | `v_` + 业务域，小写下划线 | `v_story`、`v_workhour` |
| 立方体 | 业务域 + 分析角度 | `story_delivery`、`workhour_analysis` |
| 指标 | 业务含义 + 口径词，带单位 | `total_hours`、`avg_resolve_days`、`done_rate` |
| 维度 | 与视图列同名（便于对照） | `status_name`、`department_full_name` |
| 编码/名称成对 | `xxx_code` 与 `xxx_name` | `status_code` / `status_name` |
| 知识文件 | 中文文件名，一主题一文件 | `缺陷解决周期.md` |

### 5.4 类型与返回值约定

- 指标 `type`：计数用 `BIGINT`，比率/均值/求和用 `DOUBLE`。
- 维度 `type`：`string` / `int` / `date`。
- 查询返回值的两个特点（写校验脚本时会遇到）：
  - 指标值可能是字符串，也可能是数字：同一行里 `{"staff_count":9,"active_count":"9"}`（`COUNT(*)` 出数字，`SUM()` 出字符串）。写校验脚本时统一按字符串归一化再比较；
  - 时间维度以 **epoch 毫秒**返回：`1767225600000`（即 2026-01-01）。

### 5.5 性能红线

- 所有视图都过滤 `deleted = 0`。本库多张表软删比例极高（`do_work_hour_detail_extend` 493 万行有效仅约 19.6 万行）。
- 多表关联先预聚合再 JOIN，避免笛卡尔积：（`knowledge/caveats` 第 5.1 节实测：两个预聚合子查询 JOIN 为 0.09 秒，直接多表 JOIN 会超时）。
- 大表聚合必须收窄时间范围，结果集加 `LIMIT`。
- 时间字段比较用日期边界，避免在列上套函数导致索引失效。

### 5.6 提交前自查清单

- [ ] `$env:PYTHONUTF8 = '1'` 已设置。
- [ ] `wren context validate` 无 error（`--level strict` 可再查列描述缺失）。
- [ ] `wren context build` 成功，`target/mdl.json` 已更新。
- [ ] `wren cube list` 能看到新立方体；改名/删除的已消失。
- [ ] `wren cube query --sql-only` 的 SQL 符合预期（粒度、过滤、区间）。
- [ ] 真跑一次并与业务口径对数（数字带日期）。
- [ ] 时间窗口按左闭右开书写。
- [ ] 视图列与指标名无重名。
- [ ] 每个指标/维度都有 `description`。
- [ ] 口径变化已同步 glossary / rules / metrics / caveats / sql。
- [ ] 提交信息符合 `feat(semantic): ...` / `docs(knowledge): ...` 风格。

### 5.7 报错速查

| 报错 | 原因 | 修法 |
| --- | --- | --- |
| `UnicodeDecodeError: 'gbk' codec ...` | Windows 默认编码 | 设 `PYTHONUTF8=1` |
| `view missing 'name'` | `metadata.yml` 缺 `name` | 补 `name` |
| `duplicate name 'x'` | 与已有模型/视图重名 | 改名 |
| `view missing 'statement' ...` | 没有 SQL | 在 `sql.yml` 写 `statement` |
| `cube missing 'base_object'` | 立方体未绑定视图 | 补 `base_object` |
| `base_object 'x' is not a defined model or view` | 名字拼错，或视图未 build | 核对名字并重新 build |
| `cube has no measures`（warning） | 立方体没有指标 | 至少加一个 measure |
| `hierarchies.x references unknown dimension 'y'` | 层级引用了未声明维度 | 先声明维度 |
| `table 'wren.public.v_x' not found` | 改完没 build，或引用其他视图 | 重新 build；视图不引用视图 |
| `Invalid function 'date'` | 用了 `DATE()` | 改 `CAST(x AS DATE)` |
| `Invalid function 'group_concat'` | 用了 `GROUP_CONCAT` | 见 5.2 |
| `Column in SELECT must be in GROUP BY ...` | 非聚合列未分组 | `MAX()` 包裹 + `GROUP BY` 主键 |
| 查询连到错误的库 | 项目未绑定 profile，回退全局 active | `wren context set-profile` |
| 中文显示成 `b'...'` | `table` / `csv` 输出的编码问题 | 改 `--output json` |
| `No such option: --path` | `cube` 系命令不认 `--path` | 先 `cd` 项目或用 `WREN_PROJECT_HOME` |

---

## 附录 A 命令速查

```powershell
$wren = "D:\code_work_space\llm\nl2sql\.venv\Scripts\wren.exe"
$env:PYTHONUTF8 = '1'
cd D:\code_work_space\llm\witops-wrenai          # cube 系命令必须在此目录执行
```

| 目的 | 命令 |
| --- | --- |
| 校验项目 | `& $wren context validate` |
| 严格校验（含列描述） | `& $wren context validate --level strict` |
| 编译 MDL | `& $wren context build` |
| 看项目概览 | `& $wren context show --output summary` |
| 看业务规则 | `& $wren context instructions` |
| 列出立方体 | `& $wren cube list` |
| 看立方体结构 | `& $wren cube describe <立方体>` |
| 只出 SQL 不执行 | `& $wren cube query --cube <c> --measures <m> --dimensions <d> --sql-only` |
| 真跑立方体 | `& $wren cube query --cube <c> --measures <m> --dimensions <d> --limit 10 --output json` |
| 时间维度 | `--time-dimension <维度>:<粒度>:<起>,<止>`（左闭右开） |
| 过滤 | `--filter <维度>:<op>[:<值>]`（可重复；`in`/`not_in` 用逗号分隔） |
| 直接跑 SQL | `& $wren --sql "SELECT ... " --output json` |
| 展开物理 SQL | `& $wren dry-plan --sql "SELECT ..." --datasource mysql` |
| 绑定连接 | `& $wren context set-profile <profile>` |
| 看连接解析 | `& $wren profile debug` |
| 存样例 | `& $wren memory store --nl "<问题>" --sql "<SQL>"` |
| 记忆状态 / 重建 / 查漂移 | `& $wren memory status` / `memory index` / `memory check` |

---

## 附录 B 可复制模板

### B.1 最小视图

`views/v_demo/metadata.yml`

```yaml
name: v_demo
dialect: mysql
properties:
  description: "示例视图（deleted=0，1 行/主键）"
```

`views/v_demo/sql.yml`

```yaml
statement: |
  SELECT
    t.id            AS demo_id,
    MAX(t.name)     AS demo_name
  FROM do_demo t
  WHERE t.deleted = 0
  GROUP BY t.id
```

### B.2 带枚举映射与预聚合计数的视图

```yaml
statement: |
  SELECT
    s.id                        AS story_id,
    MAX(s.status)               AS status_code,
    MAX(COALESCE(CASE s.status
      WHEN 14 THEN '已完成' WHEN 6 THEN '已取消' WHEN 99 THEN '关闭'
      ELSE CONCAT('其他(', s.status, ')')
    END, '未设置'))             AS status_name,
    MAX(p.name)                 AS project_name,
    COUNT(DISTINCT sp.project_id) AS app_project_count
  FROM do_story s
  LEFT JOIN do_project p
    ON p.id = s.project_id AND p.deleted = 0
  LEFT JOIN do_story_project sp
    ON sp.story_id = s.id AND sp.deleted = 0
  WHERE s.deleted = 0
  GROUP BY s.id
```

### B.3 最小立方体

```yaml
name: demo_analysis
description: "示例立方体 — 按状态与项目下钻"
base_object: v_demo
measures:
  - name: demo_count
    expression: "COUNT(*)"
    type: BIGINT
    description: "示例记录数"
dimensions:
  - name: status_name
    expression: "status_name"
    type: string
    description: "状态中文名"
time_dimensions:
  - name: create_time
    expression: "create_time"
    type: date
    description: "创建时间"
    levels: [year, quarter, month, day]
```

### B.4 知识条目模板

```markdown
## <口径名称>（<强制/建议>）

- 正确写法：`...`
- 错误写法：`...`（实测会得到 <错误结果>）
- 实测规模：<数字>（<日期>）
- 适用范围：<场景>
```

---

## 附录 C 术语表

| 术语 | 含义 |
| --- | --- |
| MDL | 语义模型清单，`wren context build` 生成的 `target/mdl.json`，引擎的输入 |
| model | 物理表的建模（表名、字段、类型、描述），由代码自动生成 |
| view | 语义层视图，用原生 SQL 固化过滤与派生维度；物理库中不存在 |
| cube | 立方体，绑定一个 base_object，向 AI 暴露指标与维度 |
| measure | 指标，聚合表达式（可加、可除、可比） |
| dimension | 维度，用于分组/下钻的属性 |
| time_dimension | 时间维度，支持 `year/quarter/month/week/day` 粒度 |
| `deleted = 0` | 软删过滤，本库所有统计的前提 |
| hidden | 模型字段标记，标记后 AI 与查询都不可引用 |
| dry-plan | 只做规划、不连库，把 MDL 展开成真实物理 SQL |
| profile | wren 的连接配置（`~/.wren/profiles.yml`） |
| MCP | 后端与 wren 引擎之间的工具通道（`run_sql` / `dry_run` / `dry_plan` / `query_cube` / `list_cubes`） |
