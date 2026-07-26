---
name: nl2sql-query-plan
description: "触发：策略A流水线第3步。基于子问题+Schema+WrenAI describe_model列详情，生成程序化逐步查询计划。禁止生成SQL。策略B/策略C跳过。"
---

# NL2SQL 查询计划智能体（WrenAI 增强版）

## 概述

SQL-of-Thought 流水线第3步。**仅在策略A（复杂查询）中执行。**

基于子问题分解、Schema 和 WrenAI 返回的列详情，通过 CoT 推理生成逐步执行计划。

## 输入

- 自然语言问题 (Q)
- Schema (S)
- 子问题 (C)
- WrenAI describe_model 返回的列详情（类型、约束、关系）

## 输出

程序化查询计划（文本格式），例如：

```
1. 从 titles_t 筛选 title_type='movie' 且 num_votes>10000 的行
2. 通过 principals_t.title_id 连接到 principals_t
3. 通过 principals_t.name_id 连接 names_t
4. 按 average_rating DESC 排序
5. 取前10行
```

## 关键规则

- **禁止在此阶段生成SQL** — 输出纯文本计划
- 利用 WrenAI describe_model 返回的主键/外键信息规划 JOIN
- 遵守 get_instructions 返回的业务规则
- 对每个 JOIN 说明连接列
