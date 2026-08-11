---
name: knowledge-loader
description: 从知识库中查询全量业务规则、知识库内容和指标定义等业务知识`K`
---

# NL2SQL 业务知识加载 Skill

## 概述

SQL-of-Thought 流水线第1步。从知识库中查询全量业务规则、知识库内容和指标定义等业务知识`K`，并保存，但**只保存与当前用户问题相关的内容**。

## 数据存储说明
- **存储路径**: `/workspace/nl2sql_process_data/{thread_id}/knowledge-loader/knowledge.json`
- **自动隔离**: 每个会话（thread_id）使用独立的存储目录
- **多版本支持**: 保存历史记录，便于调试和回溯
- **必须调用 write_file 写入 **

## 输入
- 自然语言问题 (Q)
- 从对话历史中获取用户消息
- 调用 read_file 读取前置 Skill 的数据
- 从 `skill_data` 获取前置 Skill 的数据

## 输出
- 必须调用 write_file 写入 `/workspace/nl2sql_process_data/{thread_id}/knowledge-loader/knowledge.json`: 过滤后的业务规则、知识库内容和指标定义、字段映射、历史问答对、常见陷阱与注意事项等

## 数据读取方式
调用 read_file 读取文件

## 执行步骤

### 步骤 1: 分析用户问题，提取关键词
从用户问题中提取：
- 业务领域（如：sales、orders、customers、inventory）
- 指标名称（如：销售额、电影、演员）
- 时间范围（如：2026年7月）
- 过滤条件（如：金额 > 1000）
- 需要显示的字段

### 步骤 2: 查询全量业务规则

调用 `list_knowledge()`发现有哪些知识文件

调用 `get_instructions()` 获取业务规则，根据关键词过滤保留相关规则

读取 `/workspace/imdb_project/knowledge/rules/*.md` 获取业务规则，根据关键词过滤保留相关规则

### 步骤 3: 查询指标定义

读取 `/workspace/imdb_project/knowledge/metrics/*.md ` 获取业务指标，根据关键词过滤保留相关业务指标

调用 `get_metrics()` 获取所有指标定义，根据关键词过滤保留相关指标

### 步骤 4: 查询字段映射

读取 `/workspace/imdb_project/knowledge/glossary/*.md ` 获取术语表，根据关键词过滤保留相关术语

### 步骤 5: 查询历史问答对

调用 `recall_queries()` 获取语义搜索匹配历史SQL，根据关键词过滤保留相关历史SQL，若匹配不到返回该项目返回空即可

### 步骤6：查询常见陷阱与注意事项

读取 `/workspace/imdb_project/knowledge/caveats/*.md ` 获取注意事项，根据关键词过滤保留相关注意事项，若匹配不到返回该项目返回空即可

### 步骤 7: 输出到文件系统

#### 输出文件: `/workspace/nl2sql_process_data/{thread_id}/knowledge-loader/knowledge.json`

- 样例如下

  | 顶层字段                | 说明         | 用途                           |
  | :---------------------- | :----------- | :----------------------------- |
  | `user_intent`           | 用户意图解析 | 记录原始问题和提取的关键信息   |
  | `entities`              | 实体提取     | 时间范围、过滤条件、显示字段   |
  | `metrics`               | 指标定义     | 相关的业务指标及其计算公式     |
  | `business_rules`        | 业务规则     | 必须应用的数据过滤和计算规则   |
  | `field_mappings`        | 字段映射     | 业务术语到物理字段的映射       |
  | ``historical_qa_pairs`` | 问答对       | 相关的历史问答对               |
  | `knowledge`             | 知识库       | 相关的业务知识和数据说明       |
  | `context_summary`       | 上下文摘要   | 核心查询上下文，供后续步骤使用 |
  | `metadata`              | 元数据       | 记录查询统计和过滤信息         |

```json
{
  "user_intent": {
    "original_question": "查询2026年7月销售额超过1000元的订单",
    "domain": "sales",
    "keywords": ["订单", "销售额", "金额", "客户名", "2026年7月"],
    "intent_type": "query_sales_performance"
  },
  "entities": {
    "time_range": {"start": "2026-07-01", "end": "2026-07-31"},
    "filters": [{"field": "amount", "operator": ">", "value": 1000}],
    "fields_to_display": ["演员"，"订单号", "金额", "客户名"]
  },
  "metrics": [
    {
      "name": "销售额",
      "aliases": ["销售金额", "成交额", "GMV"],
      "formula": "SUM(order_items.quantity * order_items.unit_price)",
      "related_tables": ["orders", "order_items"],
      "aggregation": "SUM"
    }
  ],
  "business_rules": [
    {
      "id": "rule_001",
      "name": "已取消订单不计入销售额",
      "condition": "orders.status != 'cancelled'"
    },
    {
      "id": "rule_002",
      "name": "已退款订单不计入销售额",
      "condition": "orders.refund_status != 'refunded'"
    }
  ],
  "field_mappings": {
    "订单号": "orders.order_id",
    "订单金额": "orders.amount",
    "客户名": "customers.name"
  },
   "historical_qa_pairs": [
    {
      "id": "hqa_001",
      "original_question": "查询今年6月所有订单",
      "normalized_question": "查询2026年6月所有订单",
      "sql": "SELECT o.order_id, o.amount, c.name FROM orders o LEFT JOIN customers c ON o.customer_id = c.id WHERE o.created_at BETWEEN '2026-06-01' AND '2026-06-30' AND o.status != 'cancelled'",
      "intent_type": "query_orders",
      "similarity_score": 0.72
    }
        ],
  "knowledge": [
    {
      "topic": "表关系",
      "content": "orders 表通过 customer_id 关联 customers 表"
    },
    {
      "topic": "字段说明",
      "content": "orders.status 可选值: pending, paid, shipped, delivered, cancelled"
    }
  ],
  "context_summary": {
    "main_tables": ["orders", "customers"],
    "main_fields": ["orders.order_id", "orders.amount", "customers.name"],
    "filter_conditions": ["orders.amount > 1000", "orders.status != 'cancelled'"],
    "time_range": "2026-07-01 to 2026-07-31"
  },
  "metadata": {
    "phase": "knowledge_loaded",
    "status": "success",
    "timestamp": "2026-07-28T10:00:00",
    "total_queried": {
      "metrics": 128,
      "business_rules": 523,
      "field_mappings": 2048,
      "knowledge": 156
    },
    "filtered_saved": {
      "metrics": 1,
      "business_rules": 2,
      "field_mappings": 3,
      "knowledge": 2
    }
  }
}
```

## 过滤规则

- 只保留包含用户问题关键词的规则/指标/知识
- 只保留与用户问题领域相关的内容
- 按相关性排序，每种类型最多保存 10 条

## 错误处理

- 如果全量查询失败，写入 `/workspace/nl2sql_process_data/{thread_id}/error.json`
- 错误信息: `{"error": "业务知识加载失败", "detail": "..."}`
- 若存在`/workspace/nl2sql_process_data/{thread_id}/error.json`,则追加