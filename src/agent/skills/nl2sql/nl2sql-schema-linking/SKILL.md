---
version: 0.1.0
name: nl2sql-schema-linking
description: "触发：识别查询所需的表/列/关系。输入：问题，前一个技能nl2sql-knowledge-loader的输出保存的内容。输出：裁剪后的精确Schema。跳过：策略C。"
---

# NL2SQL Schema 提取 Skill

## 概述

SQL-of-Thought 流水线第2步。根据业务知识和用户问题，自行调用 WrenAI 工具，从数据库中提取相关的表结构、字段信息和表关系，裁剪出查询所需的最小 Schema 集合。

## 数据存储说明
- **存储路径**: `/workspace/nl2sql_process_data/{thread_id}/nl2sql-schema-linking/schema.json`
- **自动隔离**: 每个会话（thread_id）使用独立的存储目录
- **依赖数据**: 读取前一个 Skill 的 `knowledge.json` 作为输入

## 输入

- 优先从对话上下文中获取前序 Skill（knowledge-loader）的输出（JSON 块）
- 若上下文中找不到，则 read_file `/workspace/nl2sql_process_data/{thread_id}/knowledge-loader/knowledge.json` 作为 fallback

## 输出

- 在回复末尾输出结构化 JSON（````json` 代码块），供编排器传递给下游 skill
- 仅在数据 >15KB 时 write_file 到 `/workspace/nl2sql_process_data/{thread_id}/nl2sql-schema-linking/schema.json` 作为 fallback

## 执行步骤

### 步骤 1: Schema 发现（并行调用 MCP 工具）

同时调用以下工具：

1. **`describe_schema()`** — 获取所有模型 Schema 的纯文本描述
2. **`get_context(question=用户问题)`** — 语义检索与问题相关的 Schema 片段
3. **`get_mdl()`** — 获取表之间的关联关系（JOIN conditions）

### 步骤 2: 按需详查（并行）

根据步骤 1 的结果，对命中的相关表并行调用 `describe_model(name)` 获取详细列信息。

- `describe_model` 必须在 `describe_schema` / `get_context` 之后（先确定哪些模型相关，再详查）

### 步骤 3: 过滤与输出

只保留与当前查询相关的字段，在回复末尾输出 JSON：

```
​```json
{
  "tables": [
    {
      "name": "orders",
      "columns": [
        {"name": "order_id", "type": "bigint", "pk": true},
        {"name": "customer_id", "type": "bigint", "fk": true},
        {"name": "amount", "type": "decimal(10,2)"},
        {"name": "status", "type": "varchar(20)"},
        {"name": "refund_status", "type": "varchar(20)"},
        {"name": "created_at", "type": "timestamp"}
      ]
    },
    {
      "name": "customers",
      "columns": [
        {"name": "id", "type": "bigint", "pk": true},
        {"name": "name", "type": "varchar(100)"}
      ]
    }
  ],
  "relations": [
    {
      "from": "orders",
      "from_field": "customer_id",
      "to": "customers",
      "to_field": "id"
    }
  ],
  "field_mappings": {
    "订单号": {"table": "orders", "field": "order_id"},
    "订单金额": {"table": "orders", "field": "amount"},
    "客户名": {"table": "customers", "field": "name"}
  },
  "metadata": {
    "phase": "schema_extracted",
    "status": "success"
  }
}
```

## 错误处理

- 如果某个表不存在，写入 `/workspace/nl2sql_process_data/{thread_id}/error.json`
- 错误信息: `{"error": "Schema 提取失败", "detail": "表 orders 不存在"}`
- 若存在`/workspace/nl2sql_process_data/{thread_id}/error.json`,则追加

## 并行策略

- `describe_schema` + `get_context` + `get_mdl` 并行（步骤 1）
- 多个 `describe_model` 并行（步骤 2，按需详查）
- `describe_model` 必须在 `describe_schema` / `get_context` 之后（先确定相关模型）

## 关键规则

- 只保留 query 中明确提到的列
- 包含 JOIN 所需的 FOREIGN KEY 列（即使未提及）
- 包含 PRIMARY KEY（用于去重/排序）
- 遵守 get_instructions 中的约束（如数值范围、NULL处理）
