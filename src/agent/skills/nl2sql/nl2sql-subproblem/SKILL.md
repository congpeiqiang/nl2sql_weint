---
name: nl2sql-subproblem
description: "触发：策略A流水线第2步。将问题分解为子句级子问题（SELECT列表/JOIN/WHERE/GROUP BY/HAVING/ORDER BY）。输出结构化JSON。策略B或策略C跳过此步骤。"
---

# NL2SQL 子问题分解智能体

## 概述

SQL-of-Thought 流水线第2步。**仅在策略A（复杂查询）中执行。**
策略B（快速通道）和策略C（Cube通道）跳过。

将复杂问题分解为子句级结构化组件，便于下游计划阶段逐步推理。

## 输入

- 自然语言问题 (Q)
- nl2sql-schema-linking 输出的裁剪 Schema (S)

## 输出

结构化JSON，每个子句一个键值对：

```json
{
  "SELECT": "列出需要查询的列",
  "FROM": "涉及的表及别名",
  "JOIN": {"table": "关联表", "on": "连接条件"},
  "WHERE": "筛选条件列表",
  "GROUP_BY": "分组列",
  "HAVING": "分组后筛选",
  "ORDER_BY": "排序列及方向",
  "LIMIT": "限制行数"
}
```

## 策略跳过规则

| 策略 | 是否执行 | 原因 |
|------|:------:|------|
| A (标准) | 是 | 复杂查询需要分解 |
| B (快速) | 否 | 简单查询直接生成SQL |
| C (Cube) | 否 | 不经过NL2SQL流水线 |
