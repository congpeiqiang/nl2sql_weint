---
name: nl2sql-schema-linking
description: "触发：识别查询所需的表/列/关系。前提：已完成WrenAI Phase 0（get_context + describe_model）。输入：WrenAI返回的Schema片段+问题。输出：裁剪后的精确Schema。跳过：策略C。"
---

# NL2SQL Schema 关联智能体（WrenAI 增强版）

## 概述

SQL-of-Thought 流水线第1步。接收 WrenAI 语义层返回的 Schema 片段，裁剪出查询所需的最小 Schema 集合。

## 输入

- 自然语言问题 (Q)
- WrenAI get_context(question) 返回的语义片段
- WrenAI describe_model 返回的列详情
- WrenAI get_instructions 返回的业务规则
- WrenAI recall_queries 返回的历史查询记录

## 输出

裁剪后的 Schema (S)：表名、列名、主键、外键、JOIN 关系

## 执行流程

```
WrenAI Phase 0 输出
  │
  ├─ list_knowledge()               → 发现有哪些知识文件
  ├─ get_context(question)          → 语义相关模型/列
  ├─ 读取 metrics/*.md               → 业务指标定义
  ├─ 读取 读取 rules/*.md             → 业务规则
  ├─ 读取 glossary/*.md              → 术语表
  ├─ wren recall_queries()          → 语义搜索匹配历史SQL
  ├─ describe_model(name1)          → 列详情 + 主键
  └─ get_instructions()             → 业务约束
  └─ recall_queries()               → 历史查询记录
        │
        ▼
  nl2sql-schema-linking
        │
        ├─ 从 WrenAI 返回中提取相关表名
        ├─ 匹配列名与问题中的实体
        ├─ 识别 FOREIGN KEY 关系
        └─ 输出最小 Schema 集合 (S)
              │
              ▼
         nl2sql-subproblem
```

## 关键规则

- 只保留 query 中明确提到的列
- 包含 JOIN 所需的 FOREIGN KEY 列（即使未提及）
- 包含 PRIMARY KEY（用于去重/排序）
- 遵守 get_instructions 中的约束（如数值范围、NULL处理）
