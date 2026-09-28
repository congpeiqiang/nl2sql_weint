---
version: 0.1.0
name: wren-clarify
description: "触发：wren-orchestrator 步骤(2)，取料完成后、生成前。基于取料包对用户问题做清晰度裁决（VAGUE/MISSING_CONDITION/TERM_AMBIGUOUS/MULTI_CHOICE）；不清晰 → 按标准格式追问并停止本轮。零额外工具调用，防过度追问五条硬规则。"
---

# 用户澄清

## 概述

官方 SOP 无澄清步骤（默认调用方已问清楚）；本扩展步以**零工具调用**方式基于取料包裁决——不重新检索。判据与 `[需要澄清]` 输出契约由本技能自持，不依赖其它编排器。

## 输入

- 原始问题 + `retrieval.json`（四块料）
- 若委派上下文已含【补充信息】（用户上轮回答过）→ 按补充信息合并裁决，仍缺才问

## 裁决判据（命中任一即 clear=false）

| reason | 判定信号（基于取料包，不再检索） |
|--------|---------|
| `VAGUE` | structure 无任何模型命中 / 问题不像数据查询 |
| `MISSING_CONDITION` | 命中模型/列，但关键过滤字段（时间范围、实体名、分组维度）问题里没给 |
| `TERM_AMBIGUOUS` | glossary/metrics 里同一术语存在多个口径 |
| `MULTI_CHOICE` | structure 命中 ≥2 个互斥模型/维度列，各自都"像" |
| `OK` | 单一模型 + 列明确映射 |

## 防过度追问（硬性规则）

1. **有据才猜**：仅当 rules/knowledge 明确给出该场景默认口径（如"未指定时间默认当前财年"）→ `clear=true` 并在 `assumption` 注明依据。
2. **无据不猜**：无明确默认规则，或存在 ≥2 种合理解释（即使一种明显占优）→ 一律 `clear=false`，解释作选项（≤3 个）追问。
3. **只问关键缺口**：issues 最多 1~3 个；不问与数据结果无关的偏好/格式。
4. **一轮上限**：已带【补充信息】仍判不清 → 带 assumption 继续，不再追问。
5. 清晰时也必须落裁决记录（assumption 留痕），不得跳过。

## 输出

clear=true：write_file `/workspace/nl2sql_process_data/{thread_id}/skill_sop/wren-clarify/verdict.json` 后交回编排器继续。

clear=false：回复以下列格式开头并**停止本轮**（不调任何生成/执行工具）：

```
[需要澄清]
【原始问题】{原样复述}
【当前数据库】{db_name}
【待补充】
1. {问题1}（选项：{A} / {B}）？
```

用户补充后由编排器**重新走 (1) 取料起**的整轮（不缓存上轮取料结果给下游）。
