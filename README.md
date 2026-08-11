# NL2SQL — 基于 SQL-of-Thought 的多智能体自然语言转 SQL 系统

基于 **WrenAI 语义层引擎** + **Harness Engineering Agent 架构**的 NL2SQL 项目。参考论文 *"SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction"* (Chaturvedi et al., 2025)。

## 核心特性

- **SQL-of-Thought 流水线** — 7 步结构化推理：Knowledge Loader → Schema Linking → Subproblem → Query Plan → SQL Generation → Execution→ Correction
- **多智能体架构** — 主智能体（意图路由 + 异步委派）+ NL2SQL 子智能体（专业 SQL 流水线）
- **WrenAI 语义层** — 以 MCP 工具形式嵌入，提供 MDL 建模、语义检索、Cube 指标查询
- **分类法引导纠错** — 9 大类 31 小类错误分类体系，最多 3 次自动纠错循环
- **图表可视化** — 通过 Semiotic MCP 渲染 SVG 图表（柱状图、折线图等）
- **实时进度追踪** — 异步子智能体执行过程中可查看每步进度和耗时
- **技能系统** — 7个 NL2SQL 技能按需加载，覆盖完整 Text-to-SQL 流程

## 架构概览

```
前端 (Next.js UI)
  │  LangGraph API (REST/SSE)
  ▼
LangGraph API Server (port 2026)
  ├── chat_agent    → 主智能体（意图识别 → 子智能体委派 → 结果汇总）
  |    |── Semiotic MCP    → 图表渲染 SVG（15 个工具）
  
  └── nl2sql_agent  → NL2SQL 子智能体（SQL-of-Thought 流水线）
       ├── WrenAI  MCP     → 语义层、MDL、SQL 执行、知识检索（18 个工具）
       ├── sql-of-thought  → 编排nl2sql流水线
       ├── 六大nl2sql SKILL
```



```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                                   前端 (React/Vue)                                  │
│                              POST /threads/{id}/runs                                │
└─────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              LangGraph API Server                                   │
│                              http://localhost:3000                                 │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                         Checkpointer (Sqlite)                               │  │
│  │                    按 thread_id 隔离对话历史                                   │  │
│  └──────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              主 Agent (Orchestrator)                                │
│                         识别意图 → 调度工具/子 Agent                                 │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                           MCP 工具                                           │  │
│  │                      ┌─────────────┐                                        │  │
│  │                      │ semiotic MCP                                         │  │     │  │                      │             │                                        │  │ 
│  │                      │             │                                        │  │
│  │                      └─────────────┘                                        │  │
│  └──────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                        SubAgentMiddleware                                     │  │
│  │                   管理子 Agent 的生命周期和调用                                 │  │
│  └──────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                            NL2SQL 子 Agent                                          │
│                    (CompiledStateGraph，按 thread_id 隔离)                          │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                        FilesystemMiddleware                                   │  │
│  │                ThreadIsolatedBackend (文件按 thread_id 隔离)                  │  │
│  └──────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                        SkillsMiddleware                                       │  │
│  │                  自动加载 ./skills/*/SKILL.md                                 │  │
│  └──────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                           n 个 Skill (按序执行)                                │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │  │
│  │  │ 1.knowledge- │→│ 2.sql-      │→│ 3.schema-      │→│ 4.sql-   │→│          │  │
│  │  │   loader     │  │  validate  │  │  linking   │  │   generation         │  │  │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘  └────┘  │  │
│  │         ↓                  ↓                  ↓                  ↓         ↓  │  │
│  │  ┌─────────────────────────────────────────────────────────────────────────┐  │  │
│  │  │                    MCP 工具 (Wren AI)                                   │  │  │
│  │  │  wren_ask() → 自然语言转 SQL  │  wren_execute() → 执行 SQL              │  │  │
│  │  │  wren_describe() → 获取表结构                                           │  │  │
│  │  └─────────────────────────────────────────────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           目标数据库 (Mysql)                                   │
│                              实际数据查询目标                                        │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### 主智能体

- 意图识别 → 子智能体委派 → 结果汇总
- 异步子智能体管理：`start_async_task` / `check_async_task` / `cancel_async_task`
- 图表渲染（Semiotic MCP）
- 技能：意图路由、报告导出、阿里云技能搜索

### NL2SQL 子智能体

- **Phase 0**（可选）：WrenAI 语义层预处理（generate-mdl → enrich-context → context-build）
- **Phase 1**：Schema 知识准备
- **Phase 2**：顺序流水线 5 步（Schema Linking → Subproblem → Query Plan → SQL Generation → 执行）
- **Phase 3**：分类法引导纠错循环（最多 3 次）

### 三种执行策略

| 策略 | 适用场景 | 路径 |
|------|---------|------|
| **A 标准流水线** | 复杂多表 JOIN / 聚合 / 窗口函数 | Schema Linking → Subproblem → Query Plan → SQL Generation |
| **B 快速通道** | 单表 / 简单筛选 / 计数 | 跳过子问题分解，直接 Query Plan → SQL |

## 数据流图

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              用户输入 NL 查询                                        │
│                                                                                     │
│  用户: "评分最高的 10 部电视剧系列是哪些？显示标题和评分"                             │
│                                                                                     │
│  ├── 关键词: "评分最高" → ORDER BY rating DESC                                     │
│  ├── 关键词: "10 部" → LIMIT 10                                                    │
│  ├── 关键词: "电视剧系列" → 实体: tv_series 表                                      │
│  ├── 关键词: "标题和评分" → SELECT title, rating                                   │
│  └── 决策: 判断是数据查询类问题，调用nl2sql子智能体，此查询比较复杂，使用策略A（标准流水线）                                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                    策略A: 标准流水线 📋（7步完整流程）                               │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │ Step 1: nl2sql-knowledge-loader（知识加载 - 必须全量执行）                    │   │
│  │ 📂 路径: ./skills/nl2sql-knowledge-loader/SKILL.md                          │   │
│  │                                                                              │   │
│  │  ├── 🔧 工具1: get_instructions() - 获取业务规则                             │   │
│  │  │   └── 输出:                                                               │   │
│  │  │       - "tv_series表存储电视剧信息"                                        │   │
│  │  │       - "rating字段为浮点数，范围1-10"                                    │   │
│  │  │       - "rating越高表示评分越好"                                           │   │
│  │  │       - "评分最高的 = ORDER BY rating DESC"                               │   │
│  │  │                                                                           │   │
│  │  ├── 🔧 工具2: get_knowledge() - 获取知识库内容                               │   │
│  │  │   └── 输出:                                                               │   │
│  │  │       - "tv_series表包含id, title, rating, genre, year, episodes等字段"   │   │
│  │  │       - "title是电视剧名称"                                               │   │
│  │  │       - "rating是评分"                                                    │   │
│  │  │                                                                           │   │
│  │  ├── 🔧 工具3: list_metrics() - 获取指标定义                                  │   │
│  │  │   └── 输出:                                                               │   │
│  │  │       - "top_rated = 按rating排序取前N"                                   │   │
│  │  │       - "avg_rating = AVG(rating)"                                        │   │
│  │  │                                                                           │   │
│  │  ├── 📤 输出: /workspace/nl2sql_process_data/{thread_id}/knowledge/         │   │     │   │       ├── knowledge.json                                                     │   │ │   │
│  │  └── 📌 更新: state.json (phase: knowledge_loader, status: done)           │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                          │                                          │
│                                          ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │ Step 2: nl2sql-schema-linking（Schema链接 - 并行加速获取）                    │   │
│  │ 📂 路径: ./skills/nl2sql-schema-linking/SKILL.md                            │   │
│  │                                                                              │   │
│  │  ├── 🔧 并行调用（同时执行，加速Schema获取）                                  │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ ⚡ 调用1: get_context("评分最高的 10 部电视剧系列")              │   │   │
│  │  │  │   └── 语义检索 Schema 片段:                                       │   │   │
│  │  │  │       - tv_series 表结构: title, rating, genre, year            │   │   │
│  │  │  │       - rating 是评分字段                                        │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ ⚡ 调用2: get_instructions() - 业务规则（已有）                  │   │   │
│  │  │  │   └── 复用 Step 1 的结果                                         │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ ⚡ 调用3: recall_queries("评分最高的 10 部电视剧系列")            │   │   │
│  │  │  │   └── 相似 NL→SQL 示例:                                         │   │   │
│  │  │  │       - "查询评分最高的10部电影" → SELECT title, rating         │   │   │
│  │  │  │         FROM movies ORDER BY rating DESC LIMIT 10               │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  ├── 🔧 串行调用（在 get_context 之后执行）                                  │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ describe_model("tv_series") - 详查相关模型                       │   │   │
│  │  │  │   └── 返回:                                                       │   │   │
│  │  │  │       - 列: id(INT), title(VARCHAR), rating(FLOAT),             │   │   │
│  │  │  │             genre(VARCHAR), year(INT), episodes(INT)            │   │   │
│  │  │  │       - 主键: id                                                │   │   │
│  │  │  │       - 索引: rating 有索引                                      │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  ├── 📤 输出: /workspace/nl2sql_process_data/{thread_id}/schema_linking/    │   │
│  │  │   └── schema.json                                             │   │
│  │  └── 📌 更新: state.json (phase: schema_linking, status: done)           │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                          │                                          │
│                                          ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3: nl2sql-subproblem（子问题分解）                                      │   │
│  │ 📂 路径: ./skills/nl2sql-subproblem/SKILL.md                                │   │
│  │                                                                              │   │
│  │  ├── 📥 输入:                                                                │   │
│  │  │   ├── /workspace/.../schema_linking/context.json                         │   │
│  │  │   └── /workspace/.../schema_linking/schema_fragment.json                 │   │
│  │  │                                                                           │   │
│  │  ├── 📝 执行: 分解为子句级子问题                                             │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ 子问题1: 数据源选择                                                │   │   │
│  │  │  │   └── 从 tv_series 表查询数据                                     │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 子问题2: 列选择                                                    │   │   │
│  │  │  │   └── 需要 title 和 rating 列                                     │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 子问题3: 筛选条件                                                  │   │   │
│  │  │  │   └── rating IS NOT NULL (排除空值)                               │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 子问题4: 排序方式                                                  │   │   │
│  │  │  │   └── ORDER BY rating DESC (从高到低)                             │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 子问题5: 数量限制                                                  │   │   │
│  │  │  │   └── LIMIT 10 (取前10名)                                         │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  ├── 📤 输出: /workspace/nl2sql_process_data/{thread_id}/subproblem/        │   │
│  │  │   └── subproblems.json                                                  │   │
│  │  └── 📌 更新: state.json (phase: subproblem, status: done)                │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                          │                                          │
│                                          ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │ Step 4: nl2sql-query-plan（查询计划生成 - CoT推理）                          │   │
│  │ 📂 路径: ./skills/nl2sql-query-plan/SKILL.md                                │   │
│  │                                                                              │   │
│  │  ├── 📥 输入: /workspace/nl2sql_process_data/{thread_id}/subproblem/        │   │
│  │  │   └── subproblems.json                                                  │   │
│  │  │                                                                           │   │
│  │  ├── 📝 执行: 生成程序化查询计划（Chain-of-Thought推理）                      │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ 查询计划:                                                          │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 1. FROM tv_series                                                 │   │   │
│  │  │  │    └── 数据源: tv_series 表                                        │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 2. WHERE rating IS NOT NULL                                       │   │   │
│  │  │  │    └── 过滤掉没有评分的记录                                         │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 3. SELECT title, rating                                           │   │   │
│  │  │  │    └── 只选择标题和评分两列                                         │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 4. ORDER BY rating DESC                                           │   │   │
│  │  │  │    └── 按评分降序排列                                               │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 5. LIMIT 10                                                       │   │   │
│  │  │  │    └── 只返回前10条记录                                             │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 执行顺序: 1 → 2 → 3 → 4 → 5                                       │   │   │
│  │  │  │ 预期行数: 10行                                                     │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  ├── 📤 输出: /workspace/nl2sql_process_data/{thread_id}/query_plan/        │   │
│  │  │   └── query_plan.txt                                                        │   │
│  │  └── 📌 更新: state.json (phase: query_plan, status: done)                │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                          │                                          │
│                                          ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │ Step 5: nl2sql-sql-generation（SQL生成）                                     │   │
│  │ 📂 路径: ./skills/nl2sql-sql-generation/SKILL.md                            │   │
│  │                                                                              │   │
│  │  ├── 📥 输入:                                                                │   │
│  │  │   ├── /workspace/.../schema_linking/*                                    │   │
│  │  │   ├── /workspace/.../subproblem/subproblems.json                         │   │
│  │  │   └── /workspace/.../query_plan/plan.json                               │   │
│  │  │                                                                           │   │
│  │  ├── 📝 执行: 根据查询计划合成可执行SQL                                      │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ 生成的 SQL:                                                        │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ SELECT title, rating                                              │   │   │
│  │  │  │ FROM tv_series                                                    │   │   │
│  │  │  │ WHERE rating IS NOT NULL                                         │   │   │
│  │  │  │ ORDER BY rating DESC                                             │   │   │
│  │  │  │ LIMIT 10;                                                        │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 验证:                                                              │   │   │
│  │  │  │ ├── 表名正确: tv_series ✓                                        │   │   │
│  │  │  │ ├── 列名正确: title, rating ✓                                    │   │   │
│  │  │  │ ├── 语法正确: SELECT...FROM...WHERE...ORDER BY...LIMIT ✓        │   │   │
│  │  │  │ └── 无 SQL 注入风险 ✓                                            │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  ├── 📤 输出: /workspace/nl2sql_process_data/{thread_id}/sql_generation/    │   │
│  │  │   └── sql.sql                                                          │   │
│  │  └── 📌 更新: state.json (phase: sql_generation, status: done)            │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                          │                                          │
│                                          ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │ Step 6: run_sql(sql)（SQL执行）                                              │   │
│  │ 🔧 工具: WrenAI run_sql()                                                   │   │
│  │                                                                              │   │
│  │  ├── 📥 输入: /workspace/nl2sql_process_data/{thread_id}/sql_generation/    │   │
│  │  │   └── sql.sql                                                           │   │
│  │  │                                                                           │   │
│  │  ├── 📝 执行:                                                               │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ run_sql("SELECT title, rating                                     │   │   │
│  │  │  │          FROM tv_series                                           │   │   │
│  │  │  │          WHERE rating IS NOT NULL                                │   │   │
│  │  │  │          ORDER BY rating DESC                                    │   │   │
│  │  │  │          LIMIT 10")                                              │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  │  执行结果:                                                               │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ {                                                                 │   │   │
│  │  │  │   "rows": [                                                        │   │   │
│  │  │  │     {"title": "绝命毒师", "rating": 9.5},                         │   │   │
│  │  │  │     {"title": "权力的游戏", "rating": 9.3},                       │   │   │
│  │  │  │     {"title": "黑道家族", "rating": 9.2},                         │   │   │
│  │  │  │     {"title": "老友记", "rating": 9.0},                           │   │   │
│  │  │  │     {"title": "生活大爆炸", "rating": 8.9},                       │   │   │
│  │  │  │     {"title": "破产姐妹", "rating": 8.8},                         │   │   │
│  │  │  │     {"title": "毒枭", "rating": 8.8},                             │   │   │
│  │  │  │     {"title": "怪奇物语", "rating": 8.7},                         │   │   │
│  │  │  │     {"title": "王冠", "rating": 8.6},                             │   │   │
│  │  │  │     {"title": "广告狂人", "rating": 8.5}                          │   │   │
│  │  │  │   ],                                                              │   │   │
│  │  │  │   "row_count": 10,                                                │   │   │
│  │  │  │   "execution_time": "0.28s"                                       │   │   │
│  │  │  │ }                                                                 │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  ├── 📤 输出: /workspace/nl2sql_process_data/{thread_id}/execution/         │   │
│  │  │   └── result.json                                                      │   │
│  │  └── 📌 更新: state.json (phase: execution, status: done)                 │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                          │                                          │
│                                          ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │ Step 7: nl2sql-correction（纠错循环 - 可选）                                  │   │
│  │ 📂 路径: ./skills/nl2sql-correction/SKILL.md                                │   │
│  │                                                                              │   │
│  │  ├── 📥 输入: /workspace/nl2sql_process_data/{thread_id}/execution/         │   │
│  │  │   └── result.json                                                       │   │
│  │  │                                                                           │   │
│  │  ├── 📝 检查执行结果:                                                       │   │
│  │  │                                                                           │   │
│  │  │  ┌───────────────────────────────────────────────────────────────────┐   │   │
│  │  │  │ ✅ 执行成功                                                         │   │   │
│  │  │  │ ├── 无语法错误                                                      │   │   │
│  │  │  │ ├── 无运行时错误                                                    │   │   │
│  │  │  │ ├── 返回10行数据                                                    │   │   │
│  │  │  │ └── 结果符合预期                                                    │   │   │
│  │  │  │                                                                   │   │   │
│  │  │  │ 决策: 跳过纠错，直接返回结果 ✅                                     │   │   │
│  │  │  └───────────────────────────────────────────────────────────────────┘   │   │
│  │  │                                                                           │   │
│  │  └── 📌 更新: state.json (phase: correction, status: skipped)              │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              返回查询结果给用户                                       │
│                                                                                     │
│  ✅ 查询完成！                                                                      │
│                                                                                     │
│  📊 评分最高的 10 部电视剧系列:                                                     │
│                                                                                     │
│  | 排名 | 标题           | 评分   |                                                │
│  |------|---------------|--------|                                                │
│  | 1    | 绝命毒师       | 9.5    |                                                │
│  | 2    | 权力的游戏     | 9.3    |                                                │
│  | 3    | 黑道家族       | 9.2    |                                                │
│  | 4    | 老友记         | 9.0    |                                                │
│  | 5    | 生活大爆炸     | 8.9    |                                                │
│  | 6    | 破产姐妹       | 8.8    |                                                │
│  | 7    | 毒枭           | 8.8    |                                                │
│  | 8    | 怪奇物语       | 8.7    |                                                │
│  | 9    | 王冠           | 8.6    |                                                │
│  | 10   | 广告狂人       | 8.5    |                                                │
│                                                                                     │
│  📝 执行SQL:                                                                        │
│  ```sql                                                                             │
│  SELECT title, rating                                                                │
│  FROM tv_series                                                                     │
│  WHERE rating IS NOT NULL                                                           │
│  ORDER BY rating DESC                                                               │
│  LIMIT 10;                                                                          │
│  ```                                                                                │
│                                                                                     │
│  💡 需要进一步分析吗？                                                              │
│  ├── 按类型查看评分排名?                                                            │
│  ├── 查看某部剧的详细信息?                                                          │
│  └── 查看评分趋势?                                                                  │
└─────────────────────────────────────────────────────────────────────────────────────┘
```



## 技术栈

| 层次 | 技术 |
|------|------|
| LLM | DeepSeek deepseek-v4-flash（via `langchain-deepseek`） |
| Agent 框架 | `deepagents` ≥ 0.6.12 |
| 编排运行时 | LangGraph API ≥ 0.11.1（uvicorn） |
| MCP 协议 | `langchain-mcp-adapters` + `fastmcp` ≥ 3.4.4 |
| 语义层 | WrenAI ≥ 0.13.0 |
| 图表 | Semiotic MCP（`npx semiotic-mcp`） |
| 数据库 | MySQL（主要）、SQLite，支持 10 种数据库引擎 |
| 可观测性 | LangSmith tracing |
| 前端 | Next.js + TypeScript + Tailwind CSS（独立项目） |

## 项目结构

```
nl2sql/
├── start_server.py                # 服务启动脚本（读取 graph.json）
├── graph.json                     # LangGraph 图注册（start_server.py 使用）
├── langgraph.json                 # LangGraph 图注册（langgraph dev 使用）
├── pyproject.toml                 # 项目依赖
├── .env                           # 环境配置
├── ARCHITECTURE.md                # 详细架构文档
│
├── docs/                          # 文档和参考资料
│   ├── agent优化记录/              # 架构优化方案
│   ├── imdb数据库/                 # IMDB schema + 测试计划
│   ├── chinook数据库/              # Chinook DDL + 测试
│   └── 论文SQL-of-Thought/        # 论文 PDF + 中文翻译
│
└── src/
    ├── agent/                     # Agent 核心
    │   ├── main_agent.py          # 主智能体
    │   ├── nl2sql_agent.py        # NL2SQL 子智能体
    │   ├── checkpointer_factory.py # 自定义 Checkpointer（SqliteSaver）
    │   ├── llms/model.py          # LLM 模型工厂
    │   ├── tools/mcp_tool.py      # MCP 多服务器客户端
    │   ├── prompt/                # 系统提示词
    │   │   ├── MAIN_AGENT_PROMPT.md
    │   │   └── NL2SQL_SYSTEM_PROMPT.md
    │   ├── subagents/
    │   │   ├── configs/nl2sql.yaml    # 子智能体配置
    │   │   ├── track_progress.py      # 进度追踪中间件
    │   │   └── check_progress.py      # check_async_task 增强补丁
    │   ├── skills/                # 技能定义（SKILL.md）
    │   │   ├── main/              # 主智能体技能
    │   │   └── nl2sql/            # NL2SQL 技能（6 个）
    │   ├── backends/              # 沙箱后端
    │   ├── utils/path_resolver.py # 工具包装器
    │   └── workspace/             # 运行时工作空间
    │
    └── mcp_server/                # 独立 DB MCP 服务器
        └── db_mcp_server/
            ├── db/db_server.py    # FastMCP 服务
            └── db/engine/         # 10 种数据库 Runner
```

### NL2SQL 技能列表

| 技能 | 描述 |
|------|------|
| `sql-of-thought` | 编排器，策略 A/B/C 路由 |
| `nl2sql-schema-linking` | Schema 发现与裁剪 |
| `nl2sql-subproblem` | 子句级问题分解 |
| `nl2sql-query-plan` | CoT 查询计划（禁止输出 SQL） |
| `nl2sql-sql-generation` | SQL 生成 |
| `nl2sql-correction` | 错误分类法引导纠错 |

## 快速开始

### 前置条件

- Python ≥ 3.13
- Node.js（用于 Semiotic MCP）
- [uv](https://docs.astral.sh/uv/) 包管理器
- MySQL 数据库（或 SQLite 文件）
- WrenAI MCP（通过 `wrenai` 包安装）

### 安装

```bash
# 克隆项目
cd nl2sql

# 安装依赖
uv sync

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 LLM API Key、数据库连接信息等
```

### 启动

```bash
python start_server.py
```

启动后：

| 地址 | 说明 |
|------|------|
| `http://localhost:2026` | API 服务 |
| `http://localhost:2026/docs` | API 文档 |
| `http://localhost:2026/ui` | LangGraph Studio UI |
| `http://localhost:2026/ok` | 健康检查 |

或使用 LangGraph CLI：

```bash
langgraph dev --port 2026
```

## 配置

### .env 主要配置项

```env
# AI 模型
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash

# LangSmith 可观测性
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_pt_xxx
LANGSMITH_PROJECT=nl2sql

# WrenAI
WREN_PROJECT_PATH=D:\path\to\wrenai_project
```

### graph.json / langgraph.json — LangGraph 图注册与持久化

两个文件内容一致，分别由不同入口读取：
- `start_server.py` → 读 `graph.json`
- `langgraph dev` → 读 `langgraph.json`

```json
{
  "graphs": {
    "chat_agent":   { "path": "./src/agent/main_agent.py:agent" },
    "nl2sql_agent": { "path": "./src/agent/nl2sql_agent.py:agent" }
  },
  "checkpointer": {
    "backend": "custom",
    "path": "./src/agent/checkpointer_factory.py:checkpointer"
  },
  "env": ".env"
}
```

#### 自定义 Checkpointer

`langgraph_api` 默认使用内存（`InMemorySaver`）做 checkpoint 持久化。如需替换为 SQLite / PostgreSQL / Redis 等后端，通过 `checkpointer` 字段配置即可，**不需要修改任何第三方包源码**。

**原理**：CLI 启动时将 `checkpointer` 配置序列化为 `LANGGRAPH_CHECKPOINTER` 环境变量 → API 层的 `_adapter.collect_checkpointer_from_env()` 自动加载并注入到所有 graph 中。

**关键约束**：
- graph 定义中（如 `main_agent.py`）**不能**传 `checkpointer=` 参数给 `create_deep_agent()`，否则 `langgraph dev` 模式会拒绝加载（`local_dev` 校验机制）。checkpointer 必须且只能在 API 层配置。
- `langgraph_api` 是纯异步运行时，checkpointer **必须实现异步方法**（`aput`、`aget_tuple` 等）。同步 `SqliteSaver` 会抛出 `NotImplementedError`，必须使用 `AsyncSqliteSaver`。
- 如果使用自定义启动脚本（如 `start_server.py`），需要从 `graph.json` 读取 `checkpointer` 字段并设置 `LANGGRAPH_CHECKPOINTER` 环境变量，否则 API 层不会加载自定义 checkpointer。

**checkpointer_factory.py 示例**（AsyncSqlite）：

```python
from pathlib import Path
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_CHECKPOINT_DB = str(Path(__file__).parent / "workspace" / "checkpoints.sqlite")

# 导出异步上下文管理器，langgraph_api 的 _yield_checkpointer() 会自动处理
# AsyncSqliteSaver 会自动调用 setup() 创建数据库表
checkpointer = AsyncSqliteSaver.from_conn_string(_CHECKPOINT_DB)
```

导出的变量可以是：
- `BaseCheckpointSaver` 实例
- 异步上下文管理器（yield `BaseCheckpointSaver`，如上例）
- 返回 `BaseCheckpointSaver` 的无参函数或协程

**自定义 checkpointer 需要实现的异步方法**：

| 方法 | 必需 | 说明 |
|------|------|------|
| `aget_tuple` | ✅ | 获取最新 checkpoint |
| `aput` | ✅ | 写入 checkpoint |
| `aput_writes` | ✅ | 写入中间结果 |
| `aget` | ✅ | 按 ID 获取 checkpoint |
| `alist` | ✅ | 列出 checkpoint 历史 |
| `adelete_thread` | 推荐 | 删除 thread（缺失则 `DELETE /threads/<id>` 不可用） |
| `adelete_for_runs` | 推荐 | 按 run_id 清理（缺失则 `rollback` 策略不可用） |
| `acopy_thread` | 可选 | 复制 thread（缺失时使用通用回退实现） |
| `aprune` | 可选 | 历史裁剪（缺失时旧 checkpoint 会持续累积） |

> **提示**：`langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` 已实现上述所有必需和推荐方法。`langgraph.checkpoint.postgres.aio.AsyncPostgresSaver` 同样可用。

#### 自定义 Store

同样机制支持自定义长期记忆 Store（替换默认的 `InMemoryStore`）：

```json
{
  "store": {
    "path": "./src/agent/store_factory.py:store"
  }
}
```

**store_factory.py 示例**：

```python
from langgraph.store.memory import InMemoryStore

store = InMemoryStore()
```

导出的变量可以是 `BaseStore` 实例、无参工厂函数、或异步上下文管理器。

> **注意**：自定义 Store 会替换默认的 Postgres + pgvector Store，向量搜索和 TTL 等功能可能不可用，取决于自定义实现。

### nl2sql.yaml — 子智能体配置

定义 NL2SQL 子智能体的工具集、技能路径和系统提示词：

```yaml
name: nl2sql
description: NL2SQL 查询专家
tools:
  - run_sql, dry_run, dry_plan, query_cube    # WrenAI 核心工具
  - list_models, describe_model, get_context   # Schema 检索
  - suggestCharts, renderInteractiveChart      # 图表工具
  - ...
skills:
  - /workspace/skills/nl2sql/
system_prompt_file: prompt/NL2SQL_SYSTEM_PROMPT.md
```

## MCP 工具

### WrenAI MCP（18 个工具）

| 工具 | 功能 |
|------|------|
| `run_sql` | 通过语义层执行 SQL |
| `dry_run` | 验证 SQL 语法 |
| `dry_plan` | MDL SQL → 真实 SQL 转换 |
| `query_cube` | Cube 指标查询 |
| `list_models` / `describe_model` | 语义模型管理 |
| `get_context` | 语义 Schema 检索 |
| `recall_queries` | 相似 NL→SQL 示例检索 |
| `get_instructions` | 业务规则加载 |

### Semiotic MCP（15 个工具）

`suggestCharts`、`renderChart`、`getSchema`、`diagnoseConfig`、`renderInteractiveChart` 等，用于图表推荐、渲染和验证。

### DB MCP Server（自建）

独立的多数据库 MCP 服务器，支持 10 种数据库引擎：

```bash
python -m mcp_server.db_mcp_server.db.db_server --transport http --port 8000
```

支持：MySQL、PostgreSQL、SQLite、BigQuery、Snowflake、MSSQL、Oracle、ClickHouse、Presto、DuckDB

## 进度追踪

异步子智能体执行过程中，`ProgressTrackerMiddleware` 记录每步状态转换和时间戳。通过 `check_async_task` 可查看：

```json
{
  "status": "running",
  "elapsed": "2m30s",
  "progress": "4/6 (67%)",
  "steps": [
    "✅ Schema发现 (20s)",
    "✅ SQL生成与验证 (40s)",
    "🔄 查询执行 (15s...)",
    "⬜ 结果汇总"
  ],
  "current_step": "查询执行",
  "current_action": "run_sql"
}
```

## 设计原则

1. **阶段化推理不可跳过** — 每个流水线阶段必须顺序执行
2. **Query Plan 严禁生成 SQL** — 纯文本步骤式计划，SQL 由专门阶段生成
3. **分类法引导纠错** — 基于 9 大类错误分类体系诊断修复
4. **纠错历史隔离** — 每次纠错尝试不共享前次历史
5. **结构化推理先于 SQL** — 必须先有推理过程，再输出 SQL

## 参考

- 论文：*SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction*
- 架构文档：[ARCHITECTURE.md](./ARCHITECTURE.md)
- WrenAI：https://github.com/Canner/WrenAI
- Semiotic：https://github.com/nteract/semiotic
