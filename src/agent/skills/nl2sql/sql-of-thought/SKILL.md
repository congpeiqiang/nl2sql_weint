---
name: sql-of-thought
description: "触发：用户提出数据库查询问题。三种策略：(A)标准流水线-复杂多表JOIN/聚合/窗口；(B)快速通道-单表/简单筛选/计数；(C)Cube通道-预定义指标。内部集成WrenAI语义层工具实现Schema自动发现。跳过：NoSQL/非SQL操作。"
---

# SQL-of-Thought：智能体 NL2SQL 编排器（策略增强版）

## 概述

本技能编排 NL2SQL 流水线，支持**三种执行策略**，根据问题复杂度自动选择最优路径。
集成 **WrenAI MCP 语义层**实现 Schema 自动发现，大幅减少 token 消耗。
流水线中的工具全部通过 MCP 提供，子智能体直接调用。

基于 *"SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction"* (Chaturvedi et al., 2025)。

---

## 策略决策（入口）

收到用户问题后，按以下优先级选择策略：

```
用户问题
    │
    ├─ 匹配 Cube 指标? 
    │   → 策略C: Cube通道
    │     调用: list_cubes → describe_cube → query_cube
    │     跳过: 全部 NL2SQL 流水线步骤
    │
    ├─ 单表/简单筛选/COUNT(*)/ORDER BY LIMIT?
    │   → 策略B: 快速通道
    │     步骤: get_context + recall_queries → SQL生成 → dry_run → run_sql
    │     跳过: subproblem/query-plan/correction
    │
    └─ 多表JOIN/聚合/子查询/窗口函数?
        → 策略A: 标准流水线
          完整 sql-of-thought 流水线 + WrenAI 语义层
```

### 策略A vs B vs C 对比

| 维度 | 策略A (标准) | 策略B (快速) | 策略C (Cube) |
|------|:-----------:|:-----------:|:-----------:|
| 调用WrenAI工具 | 3-5次 | 2次 | 2-3次 |
| 执行Ns2sql子步骤 | 4-6步 | 1-2步 | 0步 |
| token消耗 | 2000-6000 | 800-1500 | 500-1000 |
| 耗时 | 15-30s | 5-10s | 3-8s |

---

## 策略A：标准流水线（复杂查询）

### Phase 0: 语义层 Schema 发现（WrenAI MCP）

> **替代**传统 skill 中的描述搜索步骤。高效且精准。

```
Step 0.1: get_data_source()          → 了解SQL方言
Step 0.2: list_models()              → 可选，了解数据全景
Step 0.3: get_context(question)      → 语义检索相关模型/列（并行）
          get_instructions()          → 业务规则约束（并行）
          recall_queries(question, 3) → 相似查询示例（并行）
Step 0.4: describe_model(name)       → 对相关模型按需详查（并行）
```

**关键**：Step 0.3 的三个调用必须**并行**。Step 0.4 的多个 describe_model 必须**并行**。

### Phase 1-N: 传统 SQL-of-Thought 流水线

经过 Phase 0 获得精准 Schema 后，执行标准流水线：

```
Step 1: nl2sql-schema-linking  → 结合 WrenAI 返回的 Schema 片段，裁剪相关表/列
Step 2: nl2sql-subproblem      → 分解为子句级子问题
Step 3: nl2sql-query-plan      → 生成程序化查询计划（CoT推理）
Step 4: nl2sql-sql-generation  → 合成可执行SQL
Step 5: dry_run(sql)           → WrenAI 验证SQL
Step 6: run_sql(sql)           → WrenAI 执行
Step 7: [可选] nl2sql-correction → 失败时纠错循环
```

### 完整流程图

```
用户问题
  │
  ├─ Phase 0: WrenAI 语义层
  │   ├─ get_data_source()
  │   ├─ list_models() (可选)
  │   ├─ get_context + get_instructions + recall_queries (并行)
  │   └─ describe_model * N (并行，按需)
  │
  ├─ Phase 1: Schema Linking (传统)
  │   └─ nl2sql-schema-linking
  │
  ├─ Phase 2-3: 规划
  │   ├─ nl2sql-subproblem
  │   └─ nl2sql-query-plan
  │
  ├─ Phase 4-5: 生成+验证
  │   ├─ nl2sql-sql-generation
  │   └─ dry_run(sql)
  │
  ├─ Phase 6: 执行
  │   └─ run_sql(sql)
  │
  └─ Phase 7: 纠错（按需）
      └─ nl2sql-correction
```

---

## 策略B：快速通道（简单查询）

跳过传统流水线的大部分步骤，直接利用 WrenAI + skill 生成 SQL。

```
Step 1: recall_queries(question, 2)  → 找相似模板
Step 2: get_context(question)        → 确认表/列
Step 3: nl2sql-sql-generation        → 直接生成SQL（跳过linking/subproblem/plan）
Step 4: dry_run(sql)                 → 验证
Step 5: run_sql(sql)                 → 执行
```

**跳过**: nl2sql-schema-linking / nl2sql-subproblem / nl2sql-query-plan / nl2sql-correction

---

## 策略C：Cube通道（预定义指标）

完全不经过 NL2SQL 流水线，直接调用 WrenAI Cube API。

```
Step 1: list_cubes()                 → 列出可用 Cube
Step 2: describe_cube(name)          → 获取度量 + 维度
Step 3: query_cube(                  → 直接查询
          cube="sales_cube",
          measures=["total_sales"],
          dimensions=["region"],
          time_dimension="order_date:month",
          filters=["region:in:CN"]
        )
```

**优势**: 不需要生成SQL，不需要 dry_run，最省 token。

---

## WrenAI 工具速查

| 工具 | 用途 | 策略 |
|------|------|:---:|
| get_context(question) | 语义检索 Schema 片段 | A, B |
| get_instructions | 业务规则 | A |
| recall_queries(question) | 相似 NL→SQL 示例 | A, B |
| describe_model(name) | 模型列/主键/关系 | A |
| describe_schema | 全 Schema 文本 | A(备选) |
| list_models | 列出所有模型 | A(可选) |
| get_data_source | SQL 方言 | A(可选) |
| dry_run(sql) | 验证 SQL | A, B |
| run_sql(sql) | 执行 SQL | A, B |
| dry_plan(sql) | 预览目标方言 SQL | A, B(可选) |
| list_cubes | 列出 Cube | C |
| describe_cube(name) | Cube 定义 | C |
| query_cube | Cube 查询 | C |

---

## 并行调用规则

必须并行的场景：
1. get_context + get_instructions + recall_queries
2. 多个 describe_model 
3. 独立子查询的 run_sql

必须顺序的场景：
1. describe_model 必须在 get_context 之后
2. dry_run 必须在 run_sql 之前
3. schema-linking → subproblem → query-plan → sql-generation 顺序执行

---

## 数据库特定规则

### IMDb (imdb_project)
- 评分查询: 加 `num_votes > 1000`（可信度阈值）
- 电影查询: 加 `title_type = 'movie'`
- 性能: 始终加 LIMIT，先筛选再 JOIN
- 大数据量: titles 1266万行，names 1550万行，principals 16万行（采样集）
- 规则: 调用 get_instructions() 获取最新业务规则
