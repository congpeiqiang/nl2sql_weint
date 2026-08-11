---
name: nl2sql-correction
description: "触发：SQL执行失败或dry_run失败超过3次。策略A中使用。基于错误分类体系诊断并修复。策略B仅简单重试（不调用此skill）。策略C不经过此步骤。"
---

# NL2SQL 纠错智能体（WrenAI 增强版）

## 概述

SQL-of-Thought 流水线纠错循环。仅在 SQL 执行失败时调用。

- **策略A**: 调用此 skill 进行完整分类引导纠错（最多3轮）
- **策略B**: 简单 dry_run 修复（不调用此 skill，直接重试）
- **策略C**: 不适用

## 纠错流程

```
dry_run(sql) 失败 ≥3次
    │
    ▼
nl2sql-correction
    ├─ Step 1: 错误分类（error-taxonomy）
    ├─ Step 2: CoT 诊断推理
    ├─ Step 3: 生成纠错计划
    └─ Step 4: 生成纠正后的 SQL
          │
          ▼
    dry_run(新sql) → 成功 / 再次失败 → 重新分类
```

## 输入

- 失败的SQL
- WrenAI dry_run 返回的错误信息
- 原始 Schema (S)
- 原始问题 (Q)

## 输出

- 纠正后的 SQL
