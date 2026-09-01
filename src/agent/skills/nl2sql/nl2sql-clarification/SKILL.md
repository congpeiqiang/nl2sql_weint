---
version: 0.1.0
name: nl2sql-clarification
description: "触发：任何数据查询问题的第一步（Step 0），在执行 sql-of-thought 策略决策之前先判断问题是否清晰。覆盖四类不清晰：缺查询条件、业务术语歧义、笼统/太泛、多义二选一。输入：用户问题。输出：清晰度裁决 verdict.json；不清晰时以 [需要澄清] 格式输出追问并停止流水线。跳过：用户直接给出精确 SQL；无 Schema 上下文的基础设施故障。"
---

# NL2SQL 问题清晰度裁决 Skill

## 概述

sql-of-thought 流水线 **Step 0（前置澄清门）**。在进入策略决策（A/B/C）之前，先用 WrenAI 工具做 Schema 感知的清晰度判定。

- **清晰** → 正常进入 sql-of-thought 策略决策
- **不清晰** → 停止流水线，输出 `[需要澄清]` 追问，**不生成 SQL、不执行任何查询**

## 数据存储说明

- **存储路径**: `/workspace/nl2sql_process_data/{thread_id}/clarification/verdict.json`
- 同时写入 `/workspace/nl2sql_process_data/{thread_id}/clarification/context.json`（get_context 检索结果，供后续 schema-linking 复用）
- **自动隔离**: 每个会话（thread_id）使用独立的存储目录

## 输入

- 用户问题（来自委派 prompt 中的【任务目标】）
- 当前数据库名（来自委派 prompt 中的【数据库名称】）
- 若委派 prompt 已含【补充信息】（即用户上轮回答过澄清问题）→ 判定时按补充信息裁决，仍缺才问

## 输出

- `verdict.json`：结构化裁决（必须 write_file 写入）
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

### Step 1: Schema + 知识检索（只调 2 个工具）

1. 调用 `get_context(question)` — 语义检索与问题相关的 Schema 片段（哪个模型/列命中）
2. 调用 `get_instructions()` — 获取业务规则、指标/术语定义
3. 若两者任一失败（库离线/工具报错）→ 直接判定 `clear=true` 继续流水线（基础设施故障不阻塞查询），跳过 Step 2-4

将 `get_context` 的检索结果 write_file 到 `.../clarification/context.json`。

### Step 2: 对照判据裁决

按优先级判断，命中任一即触发（一个不清晰点即可问）：

| reason | 判定信号 |
|--------|---------|
| `VAGUE` | get_context 无任何模型命中 / 语义相似度极低，问题不像数据查询 |
| `MISSING_CONDITION` | 命中模型/列，但关键过滤字段（时间范围、实体名、分组维度）问题里没给 |
| `TERM_AMBIGUOUS` | 知识库/指标定义里同一术语有多个口径 |
| `MULTI_CHOICE` | get_context 命中多个互斥的维度列/模型，各自都"像" |
| `OK` | 单一模型+列明确映射 |

**防过度追问（硬性规则）**：

1. **倾向澄清，有据可猜**：
   - **有据才猜**：仅当 Schema 命中单一模型/列，且业务规则中明确给出该场景的 **默认口径或默认时间范围**（如：“如未指定时间，默认查询当前财年”），才可判定 `clear=true`，并在 `assumption` 中注明引用依据。
   - **无据不猜**：若缺乏明确业务默认规则，或存在 ≥2 种合理解释（即使一种明显占优），**一律判定 `clear=false`**，将可能的解释作为选项（≤3 个）放入追问，由用户确认。
   - 任一不清晰点（见判据表）即触发追问，不问与数据结果无关的偏好或格式问题。
2. **只问关键缺口**：issues 最多 1~3 个，一个问题最多 3 个选项
3. **一轮上限**：若委派 prompt 已含【补充信息】仍判不清 → 带 assumption 继续，不再追问
4. 清晰时也必须 write_file verdict.json（assumption 留痕），不得跳过

### Step 3: 输出 verdict.json

```json
{
  "clear": false,
  "reason": "MISSING_CONDITION",
  "original_question": "查询销售数据",
  "db_name": "imdb",
  "issues": [
    {
      "type": "missing_condition",
      "field": "时间范围",
      "question": "请提供要查询的时间范围（如：2024年全年、最近30天）"
    }
  ],
  "assumption": null
}
```

- `clear=true` 时：`reason="OK"`，`assumption` 写你采用的默认解释（如"按2024年全年、月活口径"）
- `issues[].type` 取值：`missing_condition` / `term_ambiguity` / `multi_choice` / `vague`

### Step 4: 分流

- `clear=true` → 回复"问题清晰，进入流水线"，然后正常加载 `sql-of-thought` 执行
- `clear=false` → **立即停止**，最终回复以 `[需要澄清]` 格式输出追问，**不要调用任何其他工具、不要生成 SQL、不要执行 dry_run/run_sql**

## 错误处理

- get_context/get_instructions 失败 → 写 `.../clarification/error.json`（`{"error": "澄清检查跳过", "detail": "..."}`），判定 clear 继续
- 若已存在 `.../clarification/error.json`，不重复写
