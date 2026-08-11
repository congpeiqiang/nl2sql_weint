---
name: sql-of-thought
description: "触发：用户提出数据库查询问题；用户希望将自然语言转换为SQL；用户提到NL2SQL、Text-to-SQL或自然语言数据库查询；用户需要使用自然语言问题来分析、查询或提取数据库中的数据。三种策略：(A)标准流水线-复杂多表JOIN/聚合/窗口；(B)快速通道-单表/简单筛选/计数；(C)Cube通道-预定义指标。内部集成WrenAI语义层工具实现Schema自动发现。跳过条件：用户询问NoSQL或非SQL数据库操作；用户直接编写SQL而不需要自然语言输入。"
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

---

## 策略A：标准流水线（复杂查询）

Schema 发现工作由 nl2sql-schema-linking 技能自行完成（调用 WrenAI 工具获取 Schema 片段），无需前置 Phase。

```
Step 1: nl2sql-knowledge-loader  → 技能,调用 WrenAI 工具从知识库中查询全量业务规则、知识库内容和指标定义等业务知识，并保存
Step 2: nl2sql-schema-linking  → 技能,调用 WrenAI 工具获取 Schema 片段，裁剪相关表/列
Step 3: nl2sql-subproblem      → 技能,分解为子句级子问题
Step 4: nl2sql-query-plan      → 技能,生成程序化查询计划（CoT推理）
Step 5: nl2sql-sql-generation  → 技能,合成可执行SQL
Step 6: run_sql(sql)           → WrenAI 执行
	- 读取/workspace/nl2sql_process_data/sql.sql获取sql，并执行
Step 7: [可选] nl2sql-correction → 失败时纠错循环
```

### 完整流程图

```
用户问题
  │
  ├─ Step 1: Knowledge Loader
  │   └─ nl2sql-knowledge-loader
  │
  ├─ Step 2: Schema Linking
  │   └─ nl2sql-schema-linking 
  │
  ├─ Step 3: subproblem
  │   └─ nl2sql-subproblem
  │
  ├─ Step 4: query plan
  │   └─ nl2sql-query-plan
  │
  ├─ Step 5: Generate SQL 
  │   └─ nl2sql-sql-generation
  │
  ├─ Step 6: Run SQL
  │   └─ run_sql(sql)
  │
  └─ Step 7: 纠错（按需）
      └─ nl2sql-correction
```

---

## 策略B：快速通道（简单查询）

跳过传统流水线的大部分步骤，Schema Linking 自行调用 WrenAI 工具获取 Schema 后直接生成 SQL。

```
Step 1: nl2sql-schema-linking        → 调用 WrenAI 工具获取 Schema，确认表/列
Step 2: nl2sql-sql-generation        → 直接生成SQL（跳过subproblem/plan）
Step 3: run_sql(sql)                 → 执行
	- 读取/workspace/nl2sql_process_data/sql.sql获取sql，并执行
```

**跳过**: nl2sql-subproblem / nl2sql-query-plan / nl2sql-correction

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

Schema Linking 阶段应充分利用并行调用加速 Schema 获取：

必须并行的场景：
1. `get_context` + `get_instructions` + `recall_queries`（语义检索三件套）
2. 多个 `describe_model`（按需详查多个模型）
3. 独立子查询的 `run_sql`

必须顺序的场景：
1. `describe_model` 必须在 `get_context` 之后（先确定哪些模型相关，再详查）
2. `dry_run` 必须在 `run_sql` 之前
3. schema-linking → subproblem → query-plan → sql-generation 顺序执行

---

## 数据库特定规则

### IMDb (imdb_project)
- 性能: 始终加 LIMIT，先筛选再 JOIN
- 规则: 调用 get_instructions() 获取最新业务规则
