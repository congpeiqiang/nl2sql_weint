---
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
```python
# 读取前一个 Skill 的知识数据
knowledge = skill_data.get_output("knowledge-loader")

# 提取关键信息
knowledge = skill_data.get_output("knowledge-loader")
user_intent = knowledge.get("user_intent", {})
entities = knowledge.get("entities", {})
metrics = knowledge.get("metrics", [])
business_rules = knowledge.get("business_rules", [])
field_mappings = knowledge.get("field_mappings", {})
historical_qa_pairs = knowledge.get("historical_qa_pairs", [])
```

## 输出

- `/workspace/nl2sql_process_data/{thread_id}/nl2sql-schema-linking/schema.json`: 表结构、字段、主键、外键、JOIN 关系

## 执行步骤

### 步骤1：获取所有的表

调用 `list_models()` 获取所有语义模型及其列数

### 步骤2：获取表结构

调用 `describe_model(name1)` 获取表结构详情

调用 `describe_schema`获取所有模型 Schema 的纯文本描述

### 步骤 3: 获取表关系

调用 `get_mdl()` 获取表之间的关联关系

### 步骤 4: 字段过滤

调用 `get_context`语义检索与问题相关的 Schema 片段

只保留与当前查询相关的字段

### 步骤 5: 输出到文件系统

#### 输出文件: `/workspace/nl2sql_process_data/{thread_id}/nl2sql-schema-linking/schema.json`

- 样例如下

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

## **并行策略**

- `get_context` + `get_instructions` + `recall_queries` 必须并行（语义检索三件套）
- 多个 `describe_model` 必须并行（按需详查多个模型）
- `describe_model` 必须在 `get_context` 之后（先确定哪些模型相关，再详查）

## 关键规则

- 只保留 query 中明确提到的列
- 包含 JOIN 所需的 FOREIGN KEY 列（即使未提及）
- 包含 PRIMARY KEY（用于去重/排序）
- 遵守 get_instructions 中的约束（如数值范围、NULL处理）
- 必须按顺序依次执行下方列出的所有工具，不得跳过任何一个。** 即使你认为某些工具返回的信息冗余或已从其他来源获知，也必须调用。每个工具提供不可替代的信息维度，跳过会导致 Schema 不完整或 SQL 生成错误。
