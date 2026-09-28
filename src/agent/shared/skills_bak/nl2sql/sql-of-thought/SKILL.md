---
version: 0.2.0
name: sql-of-thought
description: "触发：用户提出数据库查询问题；用户希望将自然语言转换为SQL；用户提到NL2SQL、Text-to-SQL或自然语言数据库查询；用户需要使用自然语言问题来分析、查询或提取数据库中的数据。三种策略：(A)标准流水线-复杂多表JOIN/聚合/窗口；(B)快速通道-单表/简单筛选/计数；(C)Cube通道-预定义指标。前段统一由 nl2sql-understand 完成清晰度裁决+知识+Schema（一次检索通道，不重复）。跳过条件：用户询问NoSQL或非SQL数据库操作；用户直接编写SQL而不需要自然语言输入。"
---

# SQL-of-Thought：智能体 NL2SQL 编排器（策略增强版，前段三合一）

## 概述

本技能编排 NL2SQL 流水线，支持**三种执行策略**，根据问题复杂度自动选择最优路径。
集成 **WrenAI MCP 语义层**实现 Schema 自动发现，大幅减少 token 消耗。
流水线中的工具全部通过 MCP 提供，子智能体直接调用。

前段（清晰度裁决 / 知识加载 / Schema 提取）已合并为单一 **`nl2sql-understand`** 技能：
策略 A/B 的第一步统一加载它，一次检索通道产出裁决 + 知识 + Schema，下游只读其产物，
**不再重复任何 get_context / get_instructions / recall_queries / describe_schema 检索**。

基于 *"SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction"* (Chaturvedi et al., 2025)。

---

## 决策（入口）

收到用户问题后，**先调用 `list_cubes()` 检查 Cube 匹配**，再按优先级选策略（Cube 路由不读任何 skill）：

```
用户问题
  │
  ├─ Step 0 路由: 调用 list_cubes() 查看可用 Cube
  │    ├─ 有 Cube 匹配用户问题 → 策略 C: Cube 通道（最优先，跳过全部 A/B 步骤）
  │    │     - 默认清晰：Cube 自带口径/度量，直接 list_cubes→describe_cube→query_cube
  │    │     - 仅当 ≥2 个 cube/维度口径相斥且无法确定时，允许对 C 通道问 ≤1 个 [需要澄清]
  │    │       （只依据 describe_cube 已见定义判断；不得调用 A/B 检索工具）
  │    └─ 无匹配 / list_cubes 返回空 → 按问题复杂度选 A/B
  │
  ├─ 单表/简单筛选/COUNT(*)/ORDER BY LIMIT?
  │    → 策略 B: 快速通道
  │      步骤: Step1 nl2sql-understand（清晰度+知识+轻 Schema）→ SQL生成 → dry_run → run_sql
  │      跳过: subproblem / query-plan / performance / correction
  │
  └─ 多表 JOIN/聚合/窗口/子查询?
       → 策略 A: 标准流水线（完整 sql-of-thought）
```

> **重要**：策略 C 优先级最高，匹配到 Cube 时**禁止**走 Strategy A/B 的 NL2SQL 流水线。
> 策略 A/B 的**第一步都是加载 `nl2sql-understand`**（read_file 其 SKILL.md 后按其子步执行：
> 先 get_context + get_instructions 裁决清晰度，`clear=false` 即输出 `[需要澄清]` 并停止，
> `clear=true` 才做知识 + Schema）。**不得跳过理解建模直接进 subproblem / sql-generation / run_sql。**

---

## 策略A：标准流水线（复杂查询）

```
Step 1: nl2sql-understand      → 一次检索通道：清晰度裁决 + 业务知识 + Schema，
                                 产出 verdict/context/knowledge/schema
Step 2: nl2sql-subproblem      → 分解为子句级子问题
Step 3: nl2sql-query-plan      → 生成程序化查询计划（CoT 推理）
Step 4: nl2sql-sql-generation  → 合成可执行 SQL + dry_run 验证（3 次失败 → correction）
Step 5: nl2sql-performance-optimization → 性能优化（dry_run 成功后、执行前）
Step 6: nl2sql-execution       → read 其 SKILL.md 后 run_sql(sql, limit?) 执行
Step 7: [可选] nl2sql-correction → 失败时纠错循环（重新生成 → dry_run）
```

> **数据传递**：各 Skill 优先从对话上下文获取前序输出（nl2sql-understand 回复末尾的
> 知识 JSON + Schema JSON），read_file fallback 指向
> `/workspace/nl2sql_process_data/{thread_id}/nl2sql-understand/`。

### 完整流程图

```
用户问题
  ├─ Step 0: list_cubes() 路由（无匹配才走 A/B）
  ├─ Step 1: 理解建模 → nl2sql-understand（裁决→知识→Schema，一次取齐）
  ├─ Step 2: subproblem → nl2sql-subproblem
  ├─ Step 3: query plan → nl2sql-query-plan
  ├─ Step 4: Generate SQL → nl2sql-sql-generation（dry_run 验证）
  ├─ Step 5: Performance Optimization → nl2sql-performance-optimization
  ├─ Step 6: Run SQL → nl2sql-execution（read 后 run_sql）
  └─ Step 7: 纠错（按需）→ nl2sql-correction
```

---

## 策略B：快速通道（简单查询）

```
Step 1: nl2sql-understand      → 清晰度裁决 + 业务知识 + 轻 Schema
                                 （免 describe_schema / get_mdl / describe_model，
                                 用 get_context 命中片段作 Schema 依据）
Step 2: nl2sql-sql-generation  → 直接生成 SQL（跳过 subproblem/plan）+ dry_run 验证
Step 3: nl2sql-execution       → read 其 SKILL.md 后 run_sql 执行
```

**跳过**: nl2sql-subproblem / nl2sql-query-plan / nl2sql-performance-optimization / nl2sql-correction
（不跳过理解建模的知识部分，仅免 Schema 详查）

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

**优势**: 不需要生成 SQL，不需要 dry_run，最省 token。

> **工具约束**：Strategy C 只使用 `list_cubes` / `describe_cube` / `query_cube` 三个工具。
> **禁止**调用 `get_context`、`get_instructions`、`recall_queries`、`get_all_knowledge`、
> `get_mdl`、`describe_schema`、`describe_model` 及任何 `run_sql`/`dry_run`——这些是给
> Strategy A/B 用的，Strategy C 不需要。

---

## WrenAI 工具速查

| 工具 | 用途 | 策略 |
|------|------|:---:|
| get_context(question) | 语义检索 Schema 片段（理解建模 Step1 轻检索） | A, B |
| get_instructions | 业务规则 / 口径定义 | A, B |
| get_all_knowledge | 全量业务知识（metrics/glossary/caveats） | A, B |
| recall_queries(question) | 相似 NL→SQL 历史示例 | A, B |
| describe_model(name) | 模型列/主键/关系 | A |
| describe_schema | 全 Schema 文本 | A |
| get_mdl | 表间 JOIN 关系 | A |
| list_models | 列出所有模型 | A(可选) |
| get_data_source | SQL 方言 | A(可选) |
| dry_run(sql) | 验证 SQL | A, B |
| run_sql(sql, limit?) | 执行 SQL | A, B |
| dry_plan(sql) | 预览目标方言 SQL | A, B(可选) |
| list_cubes | 列出 Cube | C |
| describe_cube(name) | Cube 定义 | C |
| query_cube | Cube 查询 | C |

> **工具面版本差异**：`describe_schema` / `get_mdl` / `get_all_knowledge` 仅较新的
> wren 语义服务提供；老版本工具面上可能没有（可调用 `list_knowledge` /
> `list_stored_queries` / `describe_model` / `get_context` 兜底）。缺失时**不要反复硬调**，
> 按 `nl2sql-understand` SKILL 的「能力降级契约」走。

---

## 并行调用规则

理解建模 / Schema 阶段应充分利用并行调用加速获取（**检索唯一性**：各检索工具全程各至多一次）：

必须并行的场景：
1. 理解建模 Step1：`get_context` + `get_instructions`（轻检索并行）
2. 知识加载：`list_knowledge` + `recall_queries`（clear 后并行；工具面有 `get_all_knowledge` 才用它一次读全替代 list_knowledge）
3. Schema 获取（策略 A）：`describe_schema` + `get_mdl` 并行
4. 多个 `describe_model`（按需详查多个模型）
5. 独立子查询的 `run_sql`

必须顺序的场景：
1. `describe_model` 必须在 `get_context` / `describe_schema` 之后（先确定哪些模型相关，再详查）
2. `dry_run` 必须在 `run_sql` 之前
3. 各阶段按需执行，子智能体会根据问题复杂度自动选择最优策略

---

## 通用规则

- 性能: 行数上限走 run_sql 的 limit 参数（**SQL 正文不要写 LIMIT 子句**，服务端会自动追加上限，重复会语法冲突），先筛选再 JOIN
- 规则: 业务规则/口径由 nl2sql-understand 的 get_instructions 统一获取，**下游不得重复调用**；需确认口径时从对话上下文/产物读
- **只读铁律**：只允许生成并执行 `SELECT`（含 `WITH ... SELECT`）只读查询。严禁任何 DML（INSERT/UPDATE/DELETE/REPLACE/MERGE）与 DDL（DROP/ALTER/CREATE/TRUNCATE/RENAME/GRANT/REVOKE）及 SET/USE/LOAD/COPY/CALL 等非查询语句。用户要求修改数据时拒绝并说明「本系统为只读查询系统，仅支持 SELECT 查询操作」。
