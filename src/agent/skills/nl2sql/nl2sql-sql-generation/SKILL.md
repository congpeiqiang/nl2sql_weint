---
name: nl2sql-sql-generation
description: "触发：根据查询计划生成SQL。策略A：输入查询计划+Schema→输出SQL。策略B：输入get_context+recall_queries→直接输出SQL。生成后必须 dry_run(sql) 验证。唯一生成SQL的智能体。"
---

# NL2SQL SQL 生成智能体（WrenAI 增强版）

## 概述

SQL-of-Thought 流水线中唯一生成 SQL 的智能体。

- **策略A**: 接收查询计划 + Schema → 生成 SQL → dry_run 验证 → 修复
- **策略B**: 接收 get_context + recall_queries → 直接生成 SQL → dry_run 验证

## 输入

- 策略A: 自然语言问题 (Q) + 查询计划 (P) + Schema (S)
- 策略B: 自然语言问题 (Q) + get_context 片段 + recall_queries 示例

## 输出

经过 dry_run 验证的可执行 SQL

## 执行流程

### 策略A（标准流水线）

```
查询计划 (P) + Schema (S) + 问题 (Q)
    │
    ▼
生成 SQL（基于计划逐步构建）
    │
    ▼
dry_run(sql)               ← WrenAI 验证
    │
    ├─ 成功 → Phase 6: 执行
    └─ 失败 → 分析错误 → 修复SQL → dry_run → 循环
                 │
                 └─ 3次失败 → nl2sql-correction
```

### 策略B（快速通道）

```
get_context 片段 + recall_queries 模板 + 问题 (Q)
    │
    ▼
基于模板生成 SQL（一次生成）
    │
    ▼
dry_run(sql)               ← WrenAI 验证
    │
    ├─ 成功 → Phase 6: 执行
    └─ 失败 → 简单修复 → dry_run → 成功/放弃
```

## 生成规则

- **WrenAI 模型名**: 使用带 `_t` 后缀的模型名（titles_t, names_t）
- **IMDb**: 加 `title_type = 'movie'`，加 `num_votes > 1000`
- **dry_run**: 生成后立即 dry_run，失败最多修复3次
- **纠错**: 3次 dry_run 失败 → 调用 nl2sql-correction
- 不要执行run_sql
