---
version: 0.4.0
name: nl2sql-understand
description: "触发：任何进入策略 A/B 的 NL2SQL 查询第一步（收到自然语言数据查询时先读本 skill）。覆盖：清晰度裁决（缺条件/术语歧义/过泛/多义）→ 业务知识加载（list_knowledge/recall_queries 为主通道）→ Schema 提取与裁剪（describe_schema + get_mdl，缺失时按能力降级契约用 describe_model 兜底）。输入：用户问题 + 数据库名。输出：verdict.json + 知识 JSON + 裁剪 Schema JSON。跳过：策略 C（Cube 命中，只用 list_cubes/describe_cube/query_cube）；用户直接给出精确可执行 SQL。"
---

# NL2SQL 理解建模 Skill（前段三合一）

## 概述

sql-of-thought 流水线 **Step 1（策略 A/B 统一前段）**。合并原澄清门 / 知识加载 / Schema 提取三个前段 skill 为**单次检索通道**：

- **清晰度裁决**：先轻量检索判问题是否清晰；**不清晰 → 停止**（`[需要澄清]` 追问），不浪费重检索。
- **知识建模**：加载业务规则 / 口径定义 / 历史 SQL 示例。
- **Schema 建模**：按策略 A/B 分流，裁剪查询所需最小 Schema。

**唯一性铁律**：`get_context` / `get_instructions` / `list_knowledge` / `recall_queries` 全程**各至多调用一次**；`describe_schema` / `get_mdl` / `get_all_knowledge` 属**能力性工具**（仅较新 wren 提供，老版本工具面可能没有），**存在时才至多调用一次，缺失勿反复硬调**——前段产物在 Step 1 一次取齐，下游（subproblem/query-plan/sql-generation）只读本步产物，**不再重复任何检索**。

## 数据存储说明

- **存储路径**: `/workspace/nl2sql_process_data/{thread_id}/nl2sql-understand/`
  - `verdict.json` — 清晰度裁决（**必须 write_file**）
  - `context.json` — Step 1 get_context 检索结果（必须 write_file，供 Schema 建模复用）
  - `knowledge.json` — Step 2 业务知识（仅数据 >15KB 才 write_file，否则回复末尾 JSON 块）
  - `schema.json` — Step 3 裁剪 Schema（仅数据 >15KB 才 write_file，否则回复末尾 JSON 块）
- **自动隔离**: 每个会话（thread_id）使用独立的存储目录

## 输入

- 用户问题（来自委派 prompt 中的【任务目标】）
- 当前数据库名（来自委派 prompt 中的【数据库名称】）
- 若委派 prompt 已含【补充信息】（即用户上轮回答过澄清问题）→ 判定时按补充信息裁决，仍缺才问

## 输出

- `verdict.json`：结构化裁决（必须 write_file）
- clear=true 时：回复末尾**并列输出**业务知识 JSON 与 Schema JSON 两个 ````json` 代码块（供下游从对话上下文读取）
- 不清晰时，最终回复**必须以以下格式开头**（一字不差）：

```
[需要澄清]
【原始问题】{原样复述用户问题}
【当前数据库】{db_name}
【待补充】
1. {问题1}（选项：{A} / {B}）？
2. {问题2}？
```

## 执行步骤

### Step 1: 轻量检索 + 清晰度裁决（只调 2 个工具）

1. **并行**调用 `get_context(question)`（语义检索命中的 Schema 片段）+ `get_instructions()`（业务规则/口径定义）。
2. 两者任一失败（库离线/工具报错）→ 写 error.json、按 `clear=true` 继续（基础设施故障不阻塞）；两者都失败 → 仍按 clear=true 走后续步骤，由各步容错兜底。
3. 把 `get_context` 结果 write_file 到 `.../nl2sql-understand/context.json`。

**对照判据裁决**（按优先级判断，命中任一即触发，一个不清晰点即可问）：

| reason | 判定信号 |
|--------|---------|
| `VAGUE` | get_context 无任何模型命中 / 语义相似度极低，问题不像数据查询 |
| `MISSING_CONDITION` | 命中模型/列，但关键过滤字段（时间范围、实体名、分组维度）问题里没给 |
| `TERM_AMBIGUOUS` | 知识库/指标定义里同一术语有多个口径 |
| `MULTI_CHOICE` | get_context 命中多个互斥的维度列/模型，各自都"像" |
| `OK` | 单一模型+列明确映射 |

**防过度追问（硬性规则）**：

1. **有据才猜**：仅当 Schema 命中单一模型/列，且业务规则明确给出该场景默认口径/默认时间范围（如"如未指定时间，默认查询当前财年"），才判 `clear=true` 并在 `assumption` 注明依据。
2. **无据不猜**：缺乏明确业务默认规则，或存在 ≥2 种合理解释（即使一种明显占优）→ **一律判 `clear=false`**，可能的解释作选项（≤3 个）放入追问，由用户确认。
3. **只问关键缺口**：issues 最多 1~3 个，一个问题最多 3 个选项；不问与数据结果无关的偏好或格式问题。
4. **一轮上限**：委派 prompt 已含【补充信息】仍判不清 → 带 assumption 继续，不再追问。
5. 清晰时也必须 write_file verdict.json（assumption 留痕），不得跳过。

### Step 2: clear=false → 立即停止（追问后失效）

- 写 `verdict.json`（`clear=false`）+ `context.json`，**然后停止**：回复以 `[需要澄清]` 开头输出追问。
- **不调用** describe_schema / get_mdl / describe_model / list_knowledge / recall_queries / get_all_knowledge；**不生成 SQL、不 dry_run/run_sql**；**不写** knowledge.json / schema.json。
- 本轮 pass 结束。用户补充后由编排器重新委派整轮重做（不缓存本次 get_context 结果到下游）。

### Step 3: clear=true → 知识建模（并行一次）

**并行**调用 `list_knowledge()`（列出知识文件：指标/术语/陷阱，把与本问题相关的并入业务知识 JSON）+ `recall_queries(question=用户问题, limit=5)`（get_instructions 返回已在 Step 1 上下文，**不再调**）。若工具列表里有 `get_all_knowledge`（较新 wren），可一次读全替代 list_knowledge；**老版本工具面没有它时不要反复硬调**——报一次 not found 即转 list_knowledge 通道继续。按用户问题关键词过滤保留相关内容，输出业务知识 JSON：

```json
{
  "user_intent": {"original_question": "原始用户问题", "domain": "业务领域", "keywords": ["关键词"], "intent_type": "意图类型"},
  "entities": {"time_range": {"start": "", "end": ""}, "filters": [{"field": "", "operator": "", "value": ""}], "fields_to_display": ["字段"]},
  "metrics": [{"name": "指标名", "aliases": ["别名"], "formula": "计算公式/SQL", "related_tables": ["表名"], "aggregation": "聚合方式"}],
  "business_rules": [{"id": "rule_id", "name": "规则名", "condition": "过滤条件"}],
  "field_mappings": {"业务术语": "物理字段"},
  "historical_qa_pairs": [{"id": "hqa_id", "original_question": "历史问题", "sql": "历史SQL", "similarity_score": 0.0}],
  "knowledge": [{"topic": "主题", "content": "内容"}],
  "context_summary": {"main_tables": ["主表"], "main_fields": ["主字段"], "filter_conditions": ["过滤条件"], "time_range": "时间范围"},
  "metadata": {"phase": "knowledge_loaded", "status": "success", "total_queried": {"rules": 0, "history": 0}, "filtered_saved": {"rules": 0, "history": 0}}
}
```

### Step 4: Schema 建模（按 A/B 分流）

按问题复杂度判断深度（无需编排器传参）：

- **策略 A（复杂查询：多表 JOIN / 聚合 / 窗口 / 子查询）——全量 Schema**：
  1. **并行**调用 `describe_schema()` + `get_mdl()`（表间 JOIN 关系）。
  2. 根据命中模型，**在 describe_schema/get_context 之后**对相关表并行 `describe_model(name)` 详查列。
  3. 裁剪最小 Schema JSON（只留 query 明确提到的列 + JOIN 外键 + 主键）。
  4. **能力降级契约**：若本库 wren 工具面**没有** `describe_schema`/`get_mdl`（工具列表无此名 / 调用即 not found）→ 改用确定性兜底：以 Step 1 `get_context` 命中片段 + `get_data_source()`（方言）为起点，用 `list_knowledge`/`list_stored_queries` 定位相关模型，再对命中模型逐个 `describe_model(name)` 补齐列/主键/关系，产出完整裁剪 Schema；`schema.json` 的 metadata 标 `"source": "model_fallback"`。**禁止**反复硬调缺失工具，也**禁止**因此跳过 Schema 建模直接进 SQL 生成。
- **策略 B（简单查询：单表 / 简单筛选 / COUNT）——轻 Schema**：**跳过** describe_schema / get_mdl / describe_model，直接用 Step 1 的 get_context 命中片段 + 上文业务知识作 Schema 依据。

Schema JSON 结构：

```json
{
  "tables": [{"name": "orders", "columns": [{"name": "order_id", "type": "bigint", "pk": true}]}],
  "relations": [{"from": "orders", "from_field": "customer_id", "to": "customers", "to_field": "id"}],
  "field_mappings": {"订单金额": {"table": "orders", "field": "amount"}},
  "metadata": {"phase": "schema_extracted", "status": "success"}
}
```

### Step 5: 收尾输出

clear=true 结束时在回复末尾**并列输出**业务知识 JSON + Schema JSON 两个 ````json` 块；数据 >15KB 的对应块 write_file 到 `knowledge.json` / `schema.json`。

## 并行策略

- Step 1：`get_context` + `get_instructions` 并行
- Step 3：`list_knowledge` + `recall_queries` 并行（工具面有 `get_all_knowledge` 时可用它一次读全替代 list_knowledge；缺失勿硬调）
- Step 4（A）：`describe_schema` + `get_mdl` 并行；两者缺失时按「能力降级契约」改用 `get_data_source` + `list_knowledge`/`list_stored_queries` 定位 + 多个 `describe_model` 并行补齐
- `describe_model` 必须在 `describe_schema` / `get_context`（或降级路径的检索）之后（先确定相关模型，再详查）

## 关键规则

- 检索唯一性：`get_context`/`get_instructions`/`list_knowledge`/`recall_queries` 必调各至多一次；`describe_schema`/`get_mdl`/`get_all_knowledge` 能力性工具存在才至多一次、缺失勿硬调（见「唯一性铁律」）
- 性能优先：全部用 MCP 工具（已按当前数据库自动路由），不用 read_file 自查
- 只保留与当前查询相关的知识/字段，避免全量塞入上下文

## 错误处理

- Step 1 检索失败 → 写 `.../nl2sql-understand/error.json`（`{"error": "理解建模检查跳过", "detail": "..."}`），按 clear=true 继续；已存在则追加
- Step 3/4 部分工具失败 → 用成功返回继续，缺失部分标空数组
- Step 3/4 全失败 → 回复末尾输出错误 JSON `{"error": "...失败", "detail": "..."}`
- 表不存在 → 写 `.../error.json`（`{"error": "Schema 提取失败", "detail": "表 X 不存在"}`）
