---
version: 0.1.0
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

## Step 0：澄清检查（前置门槛，必做）

收到用户问题后，**先**加载 `nl2sql-clarification` 技能做清晰度裁决，**再**进入策略决策：

1. `load_skill("nl2sql-clarification")` → 按技能流程执行（get_context + get_instructions → verdict.json）
2. 从对话上下文中获取 clarification 的裁决结果（`verdict.json`）：
   - `clear=true` → 继续下方「策略决策」
   - `clear=false` → **停止**，按该技能格式输出 `[需要澄清]` 追问，本技能结束，**不得**进入策略决策或调用任何查询工具

---

## 策略决策（入口）

收到用户问题后，**先检查 Cube 匹配**，再按优先级选择策略：

```
用户问题
    │
    ├─ Step 0: 澄清门(nl2sql-clarification)  → clear=false 则输出 [需要澄清] 停止
    │   └─ clear=true → 按下方优先级选策略
    │
    ├─ 【最先检查】调用 list_cubes() 查看可用 Cube
    │   ├─ 有 Cube 匹配用户问题？
    │   │   → 策略C: Cube通道（最优先，跳过全部 NL2SQL 步骤）
    │   └─ 无匹配 / list_cubes 返回空？
    │       → 继续下方策略选择
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

> **重要**：策略 C 优先级最高。每次查询前必须调用 `list_cubes()` 检查是否有匹配的 Cube。
> 匹配到 Cube 时**禁止**走 Strategy A/B 的 NL2SQL 流水线。

---

## 策略A：标准流水线（复杂查询）

Schema 发现工作由 nl2sql-schema-linking 技能自行完成（调用 WrenAI 工具获取 Schema 片段），无需前置 Phase。

```
Step 1: nl2sql-knowledge-loader  → 并行调用 MCP 工具获取业务知识（get_instructions + recall_queries + get_all_knowledge）
Step 2: nl2sql-schema-linking  → 并行调用 MCP 工具获取 Schema（describe_schema + get_context + get_mdl），裁剪相关表/列
Step 3: nl2sql-subproblem      → 分解为子句级子问题
Step 4: nl2sql-query-plan      → 生成程序化查询计划（CoT推理）
Step 5: nl2sql-sql-generation  → 合成可执行SQL + dry_run 验证
Step 5.5: nl2sql-performance-optimization → 性能优化（dry_run成功后、执行前）
Step 6: run_sql(sql)           → WrenAI 执行
Step 7: [可选] nl2sql-correction → 失败时纠错循环

> **数据传递**：各 Skill 优先从对话上下文获取前序输出，read_file 仅作为 fallback。
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
  ├─ Step 5.5: Performance Optimization
  │   └─ nl2sql-performance-optimization
  │
  ├─ Step 6: Run SQL
  │   └─ run_sql(sql)
  │
  └─ Step 7: 纠错（按需）
      └─ nl2sql-correction
```

---

## 策略B：快速通道（简单查询）

跳过传统流水线的大部分步骤，。

```
Step 1: nl2sql-knowledge-loader      → 并行调用 Wrenai MCP 工具获取业务知识（优先调用Wrenai MCP，不要调用dbmcp工具。）
Step 2: nl2sql-sql-generation        → 直接生成SQL（跳过subproblem/plan）+ dry_run 验证
Step 3: run_sql(sql)                 → 执行
```

**跳过**:nl2sql-schema-linking  / nl2sql-subproblem / nl2sql-query-plan / nl2sql-correction

---

## 策略C：Cube通道（预定义指标）

完全不经过 NL2SQL 流水线，直接调用 WrenAI Cube API。

```
Step 1: list_cubes()                 → 列出可用 Cube（必须先调用）
Step 2: describe_cube(name)          → 获取度量 + 维度
Step 3: query_cube(                  → 直接查询
          cube="sales_analytics",
          measures=["total_revenue"],
          dimensions=["billing_country"]
        )
```

**优势**: 不需要生成SQL，不需要 dry_run，最省 token。

> **工具约束**：Strategy C 只使用 `list_cubes` / `describe_cube` / `query_cube` 三个工具。
> **禁止**调用 `get_context`、`recall_queries`、`get_mdl`、`describe_schema`、`describe_model` 等 Schema 发现工具——这些是给 Strategy A/B 用的，Strategy C 不需要。

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
3. 各阶段按需执行，子智能体会根据问题复杂度自动选择最优策略

---

## 通用规则

- 性能: 行数上限走 run_sql 的 limit 参数（**SQL 正文不要写 LIMIT 子句**，服务端会自动追加上限，重复会语法冲突），先筛选再 JOIN
- 规则: 调用 get_instructions() 获取当前数据库的最新业务规则
- **只读铁律**：只允许生成并执行 `SELECT`（含 `WITH ... SELECT`）只读查询。严禁任何 DML（INSERT/UPDATE/DELETE/REPLACE/MERGE）与 DDL（DROP/ALTER/CREATE/TRUNCATE/RENAME/GRANT/REVOKE）及 SET/USE/LOAD/COPY/CALL 等非查询语句。用户要求修改数据时拒绝并说明「本系统为只读查询系统，仅支持 SELECT 查询操作」。
