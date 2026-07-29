---
name: nl2sql-schema-linking
description: "触发：识别查询所需的表/列/关系。输入：问题。输出：裁剪后的精确Schema。跳过：策略C。"
---

# NL2SQL Schema 关联智能体（WrenAI 增强版）

## 概述

SQL-of-Thought 流水线第1步。自行调用 WrenAI 工具获取 Schema 片段，裁剪出查询所需的最小 Schema 集合。

## 输入

- 自然语言问题 (Q)

## 输出

裁剪后的 Schema (S)：表名、列名、主键、外键、JOIN 关系

## 执行流程

Schema Linking 自行调用 WrenAI 工具获取 Schema 信息，然后裁剪出 Schema 集合。

**强制规则：必须按顺序依次执行下方列出的所有工具，不得跳过任何一个。** 即使你认为某些工具返回的信息冗余或已从其他来源获知，也必须调用。每个工具提供不可替代的信息维度，跳过会导致 Schema 不完整或 SQL 生成错误。

```
get_data_source()              → 返回配置的数据源（SQL 方言）
list_models()                  → 列出所有语义模型及其列数
list_knowledge()               → 发现有哪些知识文件
get_context(question)          → 语义相关模型/列
describe_schema()              → Schema 发现
读取 metrics/*.md               → 业务指标定义
读取 rules/*.md                → 业务规则
读取 glossary/*.md              → 术语表
recall_queries()               → 语义搜索匹配历史SQL
describe_model(name1)          → 列详情 + 主键
get_instructions()             → 业务约束
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

**并行策略**：
- `get_context` + `get_instructions` + `recall_queries` 必须并行（语义检索三件套）
- 多个 `describe_model` 必须并行（按需详查多个模型）
- `describe_model` 必须在 `get_context` 之后（先确定哪些模型相关，再详查）

## 关键规则

- 只保留 query 中明确提到的列
- 包含 JOIN 所需的 FOREIGN KEY 列（即使未提及）
- 包含 PRIMARY KEY（用于去重/排序）
- 遵守 get_instructions 中的约束（如数值范围、NULL处理）
